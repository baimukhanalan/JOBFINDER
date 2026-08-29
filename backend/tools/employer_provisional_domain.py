"""Search-backed provisional employer domains for job discovery only.

Public search is discovery evidence, never identity verification.  A proposal is
retained only when the live public homepage presents the employer identity in
title/heading/meta identity zones and the domain meaningfully overlaps the name.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools.company_domain_resolver import _PageText, _name_similarity, search_candidates
from backend.tools.company_enrichment import _get, enrich_company
from backend.tools.employer_domain_verifier import (
    RequestLimiter, _country_compatible_domain, _meaningful_domain_overlap,
)
from backend.tools.employer_sources import USER_AGENT

try:
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover
    Json = None


CONTRACT = "provisional_search_domain_v1"
CHECKPOINT_KEY = "provisional_domain"
MIN_HOMEPAGE_SCORE = 0.90
MIN_DOMAIN_OVERLAP = 0.50


def list_candidates(*, limit: int = 200, retry_transient: bool = False) -> list[dict]:
    checkpoint = (
        " AND COALESCE(m.qualification_evidence#>>'{provisional_domain,status}','')="
        "'transient'" if retry_transient else
        " AND NOT (m.qualification_evidence ? 'provisional_domain')")
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.brand_name,m.candidate_domain,m.domain_verified,
            m.identity_status,m.hiring_cohort_status,m.monitoring_status,
            c.legal_name,c.trade_name,c.canonical_name,c.country,c.states,c.source,
            c.source_external_id,c.metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND NOT m.domain_verified
            AND NULLIF(m.candidate_domain,'') IS NULL
            AND m.identity_status IN ('candidate','quarantined')
        """ + checkpoint + " ORDER BY m.mandatory_seed DESC,m.company_id LIMIT %s",
                    (max(1, min(int(limit), 10_000)),))
        return [dict(row) for row in cur.fetchall()]


def _homepage_identity(record: dict, candidate, client,
                       limiter: RequestLimiter) -> dict | None:
    response = _get(client, candidate.url, before_request=limiter.wait)
    if response is None:
        return None
    parser = _PageText()
    try:
        parser.feed(response.text or "")
    except Exception:
        return None
    identity_text = " ".join(parser.title + parser.headings)
    names = [record.get("brand_name"), record.get("legal_name"),
             record.get("trade_name")]
    score = max((_name_similarity(str(name or ""), identity_text)
                 for name in names), default=0.0)
    final_url = str(response.url)
    domain = company_db.normalize_domain(final_url)
    overlap = _meaningful_domain_overlap(record, domain)
    if (score < MIN_HOMEPAGE_SCORE or overlap < MIN_DOMAIN_OVERLAP
            or not _country_compatible_domain(record, domain)):
        return None
    return {
        "domain": domain, "homepage_url": final_url,
        "homepage_name_similarity": round(score, 4),
        "meaningful_domain_overlap": round(overlap, 4),
        "search_provider": candidate.provider,
        "search_rank": candidate.search_rank,
        "search_candidate_url": candidate.url,
    }


def evaluate(record: dict, *, client, limiter: RequestLimiter,
             retries: int = 2) -> dict:
    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidates = []
    for attempt in range(max(0, int(retries)) + 1):
        candidates = search_candidates(record, client, limiter, max_results=3)
        if candidates:
            break
        if attempt < max(0, int(retries)):
            time.sleep(min(2.0, 0.25 * (2 ** attempt)))
    if not candidates:
        return {"company_id": int(record["company_id"]), "status": "transient",
                "reason": "empty_or_throttled_public_search",
                "attempted_at": attempted_at}

    accepted = []
    homepage_reachable = False
    for candidate in candidates:
        evidence = _homepage_identity(record, candidate, client, limiter)
        if evidence:
            homepage_reachable = True
            accepted.append(evidence)
        else:
            # A direct bounded check distinguishes an identity rejection from a
            # provider/network failure without accepting arbitrary body text.
            homepage_reachable = homepage_reachable or _get(
                client, candidate.url, retries=0, before_request=limiter.wait) is not None
    by_domain = {item["domain"]: item for item in accepted}
    if len(by_domain) > 1:
        return {"company_id": int(record["company_id"]), "status": "ambiguous",
                "reason": "multiple_provisional_domains", "attempted_at": attempted_at}
    if not by_domain:
        return {"company_id": int(record["company_id"]),
                "status": "no_match" if homepage_reachable else "transient",
                "reason": ("homepage_identity_or_domain_overlap_failed"
                           if homepage_reachable else "homepage_unreachable"),
                "attempted_at": attempted_at}

    identity = next(iter(by_domain.values()))
    domain = identity["domain"]
    enriched = enrich_company(
        {"domain": domain, "legal_name": record.get("legal_name"),
         "trade_name": record.get("trade_name")}, client,
        before_request=limiter.wait)
    evidence = {
        "class": "provisional_search_official_homepage",
        "status": "provisional", "candidate_domain": domain,
        "method": "public_search+live_homepage_identity+domain_name_overlap",
        **identity, "observed_at": attempted_at,
    }
    return {
        "company_id": int(record["company_id"]), "status": "accepted",
        "reason": None, "attempted_at": attempted_at,
        "candidate_domain": domain, "domain_evidence": [evidence],
        "careers_url": enriched.get("careers_url") or None,
        "ats": enriched.get("ats") or None,
        "ats_slug": enriched.get("ats_slug") or None,
        "ats_url": enriched.get("ats_url") or None,
        "careers_confidence": enriched.get("careers_confidence"),
    }


def persist(results: list[dict]) -> dict[str, int]:
    counts = {"accepted": 0, "no_match": 0, "ambiguous": 0,
              "transient": 0, "updated": 0, "job_discovery_ready": 0}
    if not results:
        return counts
    with company_db.conn() as connection:
        cur = connection.cursor()
        try:
            for result in results:
                status = str(result.get("status") or "")
                if status not in counts or status in {"updated", "job_discovery_ready"}:
                    continue
                counts[status] += 1
                checkpoint = {
                    "contract": CONTRACT, "status": status,
                    "reason": result.get("reason"),
                    "retryable": status == "transient",
                    "attempted_at": result.get("attempted_at"),
                    "candidate_domain": result.get("candidate_domain"),
                }
                company_id = int(result["company_id"])
                if status == "accepted":
                    evidence = result.get("domain_evidence") or []
                    cur.execute("""
                      UPDATE company_employer_master SET
                        candidate_domain=COALESCE(candidate_domain,%s),
                        domain_evidence=COALESCE((SELECT jsonb_agg(e)
                          FROM jsonb_array_elements(domain_evidence) e
                          WHERE COALESCE(e->>'class','')<>
                            'provisional_search_official_homepage'),'[]'::jsonb) || %s,
                        qualification_evidence=qualification_evidence ||
                          jsonb_build_object('provisional_domain',%s),updated_at=now()
                      WHERE company_id=%s AND in_target_population
                        AND NOT domain_verified AND identity_status<>'verified'
                    """, (result["candidate_domain"],
                          Json(evidence) if Json is not None else evidence,
                          Json(checkpoint) if Json is not None else checkpoint, company_id))
                    if cur.rowcount != 1:
                        continue
                    cur.execute("""
                      UPDATE company_discovery SET domain=COALESCE(domain,%s),
                        careers_url=COALESCE(careers_url,%s),ats=COALESCE(ats,%s),
                        ats_slug=COALESCE(ats_slug,%s),ats_url=COALESCE(ats_url,%s),
                        careers_confidence=GREATEST(COALESCE(careers_confidence,0),
                          COALESCE(%s,0)),
                        provenance=provenance || jsonb_build_object(
                          'provisional_domain',%s),updated_at=now()
                      WHERE id=%s
                    """, (result["candidate_domain"], result.get("careers_url"),
                          result.get("ats"), result.get("ats_slug"), result.get("ats_url"),
                          result.get("careers_confidence"),
                          Json(checkpoint) if Json is not None else checkpoint, company_id))
                    counts["updated"] += 1
                    counts["job_discovery_ready"] += int(bool(
                        result.get("ats") and result.get("ats_slug")))
                else:
                    cur.execute("""
                      UPDATE company_employer_master SET
                        qualification_evidence=qualification_evidence ||
                          jsonb_build_object('provisional_domain',%s),updated_at=now()
                      WHERE company_id=%s AND in_target_population AND NOT domain_verified
                    """, (Json(checkpoint) if Json is not None else checkpoint, company_id))
                    counts["updated"] += cur.rowcount
        finally:
            cur.close()
    return counts


def run(*, limit: int = 200, workers: int = 4, min_interval: float = 0.5,
        checkpoint_size: int = 50, retries: int = 2,
        retry_transient: bool = False, dry_run: bool = False) -> dict:
    worker_count = max(1, min(int(workers), 4))
    batch_size = max(1, min(int(checkpoint_size), 200))
    rows = list_candidates(limit=limit, retry_transient=retry_transient)
    limiter = RequestLimiter(min_interval)
    totals = Counter()
    reasons = Counter()
    samples = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]

        def work(row):
            with httpx.Client(timeout=httpx.Timeout(15.0),
                              headers={"User-Agent": USER_AGENT}) as client:
                return evaluate(row, client=client, limiter=limiter, retries=retries)

        results = []
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(work, row): row for row in batch}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        "company_id": int(row["company_id"]), "status": "transient",
                        "reason": f"worker_error:{type(exc).__name__}",
                        "attempted_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"),
                    })
        for result in results:
            reasons[str(result.get("reason") or "accepted")] += 1
            if result.get("status") == "accepted" and len(samples) < 25:
                samples.append({key: result.get(key) for key in
                                ("company_id", "candidate_domain", "careers_url",
                                 "ats", "ats_slug")})
        if dry_run:
            saved = Counter(item["status"] for item in results)
            saved["updated"] = 0
            saved["job_discovery_ready"] = sum(
                bool(item.get("ats") and item.get("ats_slug")) for item in results)
        else:
            saved = Counter(persist(results))
        totals.update(saved)
    return {
        "selected": len(rows), "processed": len(rows),
        "accepted": totals["accepted"], "no_match": totals["no_match"],
        "ambiguous": totals["ambiguous"], "transient": totals["transient"],
        "updated": totals["updated"],
        "job_discovery_ready": totals["job_discovery_ready"],
        "workers": worker_count, "dry_run": bool(dry_run),
        "reasons": dict(sorted(reasons.items())), "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--min-interval", type=float, default=0.5)
    parser.add_argument("--checkpoint-size", type=int, default=50)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-transient", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = run(limit=args.limit, workers=args.workers,
                 min_interval=args.min_interval,
                 checkpoint_size=args.checkpoint_size, retries=args.retries,
                 retry_transient=args.retry_transient, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
