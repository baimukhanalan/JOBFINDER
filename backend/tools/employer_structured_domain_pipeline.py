"""Resumable authoritative domain proposals and strict live second-factor checks.

Structured sources may propose a domain but never verify it.  Verification requires
the exact employer identity in title, JSON-LD name, or footer/legal context on the
same-domain live site.  Search-derived domains are represented only as candidates.
This module never reads or writes careers, ATS, jobs, or application data.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools.company_enrichment import _get, canonical_domain, official_url
from backend.tools.employer_authoritative_sources import (
    fetch_sam_entities, fetch_sec_submission,
)
from backend.tools.employer_fdic_domain_verification import verify_identity_page
from backend.tools.employer_official_enrichment import fetch_fdic_enrichment


CONTRACT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def wikidata_p856_candidate(row: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = _mapping(row.get("metadata"))
    qid = _text(metadata.get("wikidata_qid")).upper()
    domain = canonical_domain(row.get("domain"))
    source_url = _text(row.get("source_url"))
    if (row.get("source") != "wikidata_employer"
            or metadata.get("official_website_property") != "P856"
            or not qid.startswith("Q") or not qid[1:].isdigit()
            or not source_url.rstrip("/").endswith("/" + qid) or not domain):
        return None
    return {
        "class": "structured_corporate_source", "provider": "wikidata_p856",
        "entity_id": f"wikidata_qid:{qid}", "candidate_domain": domain,
        "assertion": "exact_entity_official_website_P856",
        "source_url": source_url,
        "observed_at": _text(row.get("source_observed_at")) or None,
    }


def entity_assertion_candidate(row: Mapping[str, Any], node: Mapping[str, Any], *,
                               id_key: str, provider: str) -> dict[str, Any] | None:
    expected = _text(_mapping(row.get("external_ids")).get(id_key)).upper()
    entity_id = _text(node.get("entity_id"))
    prefix = {"fdic_cert": "fdic_cert:", "sec_cik": "sec_cik:",
              "sam_uei": "sam_uei:"}[id_key]
    if not expected or entity_id.upper() != (prefix + expected).upper():
        return None
    assertions = [item for item in node.get("domain_assertions") or []
                  if isinstance(item, Mapping) and item.get("entity_id") == entity_id]
    domains = {canonical_domain(item.get("domain")) for item in assertions
               if canonical_domain(item.get("domain"))}
    if len(domains) != 1:
        return None
    assertion = assertions[0]
    provenance = _mapping(assertion.get("provenance")) or _mapping(node.get("provenance"))
    return {
        "class": "structured_corporate_source", "provider": provider,
        "entity_id": entity_id, "candidate_domain": next(iter(domains)),
        "assertion": _text(assertion.get("assertion_type")) or "entity_reported_website",
        "source_url": _text(provenance.get("source_url")),
        "observed_at": _text(provenance.get("observed_at")) or None,
    }


def search_candidate(*, domain: str, source_url: str) -> dict[str, Any]:
    """Represent search output without granting a structured first factor."""
    return {"class": "search_candidate", "provider": "public_search",
            "candidate_domain": canonical_domain(domain), "source_url": source_url,
            "verification_eligible": False}


def resolve_authoritative_candidates(
    row: Mapping[str, Any], *, fdic_fetcher: Callable[[str], Mapping[str, Any]] | None = None,
    sec_fetcher: Callable[[str], Mapping[str, Any] | None] | None = None,
    sam_node: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    factors: list[dict[str, Any]] = []
    if candidate := wikidata_p856_candidate(row):
        factors.append(candidate)
    ids = _mapping(row.get("external_ids"))
    if ids.get("fdic_cert") and fdic_fetcher is not None:
        result = fdic_fetcher(_text(ids["fdic_cert"]))
        proposal = _mapping(result.get("proposed_domain_evidence"))
        if proposal and _text(proposal.get("entity_id")) == f"fdic_cert:{ids['fdic_cert']}":
            factors.append({
                "class": "structured_corporate_source", "provider": "fdic_bankfind",
                "entity_id": proposal["entity_id"],
                "candidate_domain": canonical_domain(proposal.get("domain")),
                "assertion": _text(proposal.get("assertion")) or
                             "institution_reported_primary_website",
                "source_url": _text(_mapping(proposal.get("provenance")).get("source_url")),
                "observed_at": _text(_mapping(proposal.get("provenance")).get("observed_at")),
            })
    if ids.get("sec_cik") and sec_fetcher is not None:
        node = sec_fetcher(_text(ids["sec_cik"]))
        if node and (candidate := entity_assertion_candidate(
                row, node, id_key="sec_cik", provider="sec_edgar")):
            factors.append(candidate)
    if sam_node and (candidate := entity_assertion_candidate(
            row, sam_node, id_key="sam_uei", provider="sam_gov")):
        factors.append(candidate)
    factors = [item for item in factors if item.get("candidate_domain")]
    domains = sorted({item["candidate_domain"] for item in factors})
    if len(domains) > 1:
        return {"company_id": int(row["company_id"]), "status": "quarantine",
                "reason": "authoritative_domain_conflict", "factors": factors,
                "domains": domains}
    if not domains:
        return {"company_id": int(row["company_id"]), "status": "no_candidate",
                "reason": "authoritative_source_has_no_domain", "factors": []}
    return {"company_id": int(row["company_id"]), "status": "proposed",
            "candidate_domain": domains[0], "factors": factors}


def load_proposal_rows(*, limit: int, retry: bool = False,
                       include_sam: bool = False) -> list[dict[str, Any]]:
    retry_clause = (
        " AND COALESCE(m.qualification_evidence#>>'{structured_domain_pipeline,proposal_status}','')='transient'"
        if retry else
        " AND NOT (m.qualification_evidence ? 'structured_domain_pipeline')")
    sam_clause = " OR c.external_ids ? 'sam_uei'" if include_sam else ""
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id AS company_id,c.source,c.source_external_id,c.external_ids,
            c.legal_name,c.trade_name,c.domain,c.metadata,c.source_url,
            c.source_observed_at,m.brand_name
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND (
            (c.source='wikidata_employer' AND c.metadata->>'official_website_property'='P856')
            OR c.external_ids ? 'fdic_cert' OR c.external_ids ? 'sec_cik'"""
                    + sam_clause + ")" + retry_clause + " ORDER BY c.id LIMIT %s",
                    (max(1, min(int(limit), 10_000)),))
        return [dict(row) for row in cur.fetchall()]


def persist_proposals(results: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"proposed": 0, "quarantine": 0, "no_candidate": 0,
              "transient": 0, "updated": 0}
    if not results:
        return counts
    with company_db._cur(False) as cur:
        for result in results:
            status = _text(result.get("status"))
            if status not in {"proposed", "quarantine", "no_candidate", "transient"}:
                continue
            counts[status] += 1
            checkpoint = json.dumps({"proposal_status": status,
                                     "candidate_domain": result.get("candidate_domain"),
                                     "reason": result.get("reason"),
                                     "retryable": status == "transient",
                                     "checked_at": _now(),
                                     "contract_version": CONTRACT_VERSION})
            if status == "proposed":
                factors = json.dumps(result.get("factors") or [])
                cur.execute("""
                  UPDATE company_employer_master SET candidate_domain=%s,
                    domain_evidence=COALESCE((SELECT jsonb_agg(e)
                      FROM jsonb_array_elements(domain_evidence) e
                      WHERE e->>'class'<>'structured_corporate_source'),'[]'::jsonb)
                      || %s::jsonb,
                    qualification_evidence=qualification_evidence ||
                      jsonb_build_object('structured_domain_pipeline',%s::jsonb),
                    updated_at=now()
                  WHERE in_target_population AND company_id=%s
                """, (result["candidate_domain"], factors, checkpoint,
                      int(result["company_id"])))
            else:
                cur.execute("""
                  UPDATE company_employer_master SET
                    qualification_evidence=qualification_evidence ||
                      jsonb_build_object('structured_domain_pipeline',%s::jsonb),
                    updated_at=now() WHERE in_target_population AND company_id=%s
                """, (checkpoint, int(result["company_id"])))
            counts["updated"] += cur.rowcount
    return counts


def propose_authoritative_domains(*, limit: int = 2500, min_interval: float = 0.15,
                                  retry: bool = False) -> dict[str, Any]:
    sam_key = os.environ.get("SAM_API_KEY", "").strip()
    rows = load_proposal_rows(limit=limit, retry=retry, include_sam=bool(sam_key))
    sam_nodes: dict[str, Mapping[str, Any]] = {}
    if sam_key:
        ueis = [_text(_mapping(row.get("external_ids")).get("sam_uei")) for row in rows]
        for node in fetch_sam_entities(ueis, api_key=sam_key, min_interval=min_interval):
            sam_nodes[_text(node.get("entity_id")).split(":", 1)[-1].upper()] = node
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        ids = _mapping(row.get("external_ids"))
        try:
            result = resolve_authoritative_candidates(
                row, fdic_fetcher=fetch_fdic_enrichment,
                sec_fetcher=fetch_sec_submission,
                sam_node=sam_nodes.get(_text(ids.get("sam_uei")).upper()))
        except (httpx.HTTPError, OSError, ValueError):
            results.append({"company_id": int(row["company_id"]),
                            "status": "transient",
                            "reason": "official_provider_error"})
            continue
        results.append(result)
        if min_interval > 0 and index + 1 < len(rows) and (
                ids.get("fdic_cert") or ids.get("sec_cik")):
            time.sleep(min_interval)
    persisted = persist_proposals(results)
    return {"selected": len(rows), **persisted, "sam_enabled": bool(sam_key)}


class RequestLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.1, float(interval)); self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


def load_verification_rows(*, limit: int, retry: bool = False) -> list[dict[str, Any]]:
    retry_clause = (
        " AND COALESCE(m.qualification_evidence#>>'{structured_domain_pipeline,verification_status}','')='transient'"
        if retry else
        " AND COALESCE(m.qualification_evidence#>>'{structured_domain_pipeline,verification_status}','')=''")
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id AS company_id,c.legal_name,c.trade_name,m.brand_name,
            m.candidate_domain,m.domain_evidence
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population AND NOT m.domain_verified
            AND NULLIF(m.candidate_domain,'') IS NOT NULL
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.domain_evidence) e
              WHERE e->>'class'='structured_corporate_source'
                AND lower(e->>'candidate_domain')=lower(m.candidate_domain))"""
                    + retry_clause + " ORDER BY c.id LIMIT %s",
                    (max(1, min(int(limit), 10_000)),))
        return [dict(row) for row in cur.fetchall()]


def verify_live_record(row: Mapping[str, Any], *, client: httpx.Client,
                       limiter: RequestLimiter) -> dict[str, Any]:
    domain = canonical_domain(row.get("candidate_domain"))
    response = _get(client, official_url(domain), retries=1, before_request=limiter.wait)
    if response is None:
        return {"company_id": int(row["company_id"]), "status": "transient"}
    identity = verify_identity_page(
        proposed_domain=domain, final_url=str(response.url), page_html=response.text,
        legal_name=_text(row.get("legal_name")), brand_name=_text(row.get("brand_name")),
        trade_name=_text(row.get("trade_name")))
    if not identity.get("passed"):
        return {"company_id": int(row["company_id"]), "status": "quarantine",
                "candidate_domain": domain, "identity": identity}
    return {"company_id": int(row["company_id"]), "status": "verified",
            "candidate_domain": domain, "identity": identity}


def persist_verifications(results: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"verified": 0, "quarantine": 0, "transient": 0, "updated": 0}
    with company_db._cur(False) as cur:
        for result in results:
            status = _text(result.get("status"))
            if status not in {"verified", "quarantine", "transient"}:
                continue
            counts[status] += 1
            company_id = int(result["company_id"])
            checkpoint = json.dumps({
                "verification_status": status, "candidate_domain": result.get("candidate_domain"),
                "identity": result.get("identity"), "checked_at": _now(),
                "retryable": status == "transient",
                "contract_version": CONTRACT_VERSION})
            if status == "verified":
                identity = _mapping(result.get("identity"))
                second = {
                    "class": "official_site_identity", "provider": "official_site_identity",
                    "domain": result["candidate_domain"],
                    "assertion": "exact_identity_in_title_schema_or_legal_footer",
                    "homepage_url": identity.get("final_url"),
                    "matched_name": identity.get("matched_name"),
                    "context_type": identity.get("context_type"),
                    "context_excerpt": identity.get("context_excerpt"),
                    "observed_at": _now(),
                }
                cur.execute("""
                  UPDATE company_employer_master SET domain_verified=TRUE,
                    identity_confidence=GREATEST(identity_confidence,0.98),
                    domain_evidence=domain_evidence || %s::jsonb,
                    qualification_evidence=jsonb_set(qualification_evidence,
                      '{structured_domain_pipeline}',%s::jsonb,TRUE),
                    last_verified_at=now(),updated_at=now()
                  WHERE in_target_population AND company_id=%s
                    AND lower(candidate_domain)=lower(%s)
                """, (json.dumps([second]), checkpoint, company_id,
                      result["candidate_domain"]))
                if cur.rowcount == 1:
                    cur.execute("""
                      UPDATE company_discovery SET domain=%s,domain_confidence=0.98,
                        provenance=provenance || jsonb_build_object(
                          'structured_domain_pipeline',%s::jsonb),updated_at=now()
                      WHERE id=%s
                    """, (result["candidate_domain"], checkpoint, company_id))
            else:
                cur.execute("""
                  UPDATE company_employer_master SET
                    qualification_evidence=jsonb_set(qualification_evidence,
                      '{structured_domain_pipeline}',%s::jsonb,TRUE),updated_at=now()
                  WHERE in_target_population AND company_id=%s
                """, (checkpoint, company_id))
            counts["updated"] += int(cur.rowcount > 0)
    return counts


def verify_structured_domains(*, limit: int = 100, workers: int = 4,
                              min_interval: float = 0.25,
                              retry: bool = False) -> dict[str, int]:
    rows = load_verification_rows(limit=limit, retry=retry)
    limiter = RequestLimiter(min_interval)
    results: list[dict[str, Any]] = []
    errors = 0
    def work(row):
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={
                "User-Agent": "JobFinder-structured-domain/1.0"}) as client:
            return verify_live_record(row, client=client, limiter=limiter)
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 4))) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                errors += 1
                result = {"company_id": int(futures[future]["company_id"]),
                          "status": "transient", "reason": "worker_error"}
            results.append(result)
    persisted = persist_verifications(results)
    return {"selected": len(rows), **persisted, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    proposal = sub.add_parser("propose"); proposal.add_argument("--limit", type=int, default=2500)
    proposal.add_argument("--min-interval", type=float, default=0.15)
    proposal.add_argument("--retry", action="store_true")
    verify = sub.add_parser("verify"); verify.add_argument("--limit", type=int, default=100)
    verify.add_argument("--workers", type=int, default=4)
    verify.add_argument("--min-interval", type=float, default=0.25)
    verify.add_argument("--retry", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "propose":
        result = propose_authoritative_domains(
            limit=args.limit, min_interval=args.min_interval, retry=args.retry)
    else:
        result = verify_structured_domains(
            limit=args.limit, workers=args.workers,
            min_interval=args.min_interval, retry=args.retry)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
