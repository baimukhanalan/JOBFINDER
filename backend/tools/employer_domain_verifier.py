"""Second-factor verification for structured employer domain candidates."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_master_db as master_db
from backend.tools.company_domain_resolver import (
    _PageText, _name_similarity, search_candidates, verify_candidate,
)
from backend.tools.company_enrichment import _get, enrich_company
from backend.tools.employer_sources import USER_AGENT


_GENERIC_NAME_TOKENS = {
    "and", "association", "company", "corp", "corporation", "group", "holdings",
    "inc", "incorporated", "llc", "lp", "ltd", "management", "of", "services",
    "the", "usa", "us",
}
_GENERICIZED_CC_TLDS = {"ai", "co", "io", "me", "tv"}


def _country_compatible_domain(record: dict, domain: str) -> bool:
    country = str(record.get("country") or "US").upper()
    tld = company_db.normalize_domain(domain).rsplit(".", 1)[-1]
    if country == "US" and len(tld) == 2 and tld not in _GENERICIZED_CC_TLDS | {"us"}:
        return False
    return True


def _meaningful_domain_overlap(record: dict, domain: str) -> float:
    host = company_db.normalize_domain(domain).split(".", 1)[0]
    names = [record.get("brand_name"), record.get("legal_name"), record.get("trade_name")]
    best = 0.0
    for name in names:
        tokens = {
            token for token in company_db.normalize_company_name(str(name or "")).split()
            if len(token) >= 3 and token not in _GENERIC_NAME_TOKENS
        }
        if tokens:
            best = max(best, sum(token in host for token in tokens) / len(tokens))
    return best


class RequestLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.1, float(interval))
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


def verify_record(record: dict, *, client: httpx.Client,
                  limiter: RequestLimiter) -> dict | None:
    domain = company_db.normalize_domain(record.get("candidate_domain"))
    if not domain:
        return None
    response = _get(client, f"https://{domain}/", before_request=limiter.wait)
    if response is None:
        return None
    parser = _PageText()
    try:
        parser.feed(response.text or "")
    except Exception:
        return None
    evidence_text = " ".join(parser.title + parser.headings + parser.text[:3000])
    names = [record.get("brand_name"), record.get("legal_name"), record.get("trade_name")]
    score = max((_name_similarity(str(name or ""), evidence_text) for name in names), default=0)
    # The first factor is already an exact employer-name match in the structured source.
    # The live official site must independently present that same brand/legal identity.
    if score < 0.86:
        return None
    enriched = enrich_company(
        {"domain": domain, "legal_name": record.get("legal_name"),
         "trade_name": record.get("trade_name")}, client,
        before_request=limiter.wait)
    return {
        "company_id": record["company_id"], "domain": domain,
        "careers_url": enriched.get("careers_url") or None,
        "ats": enriched.get("ats") or None, "ats_slug": enriched.get("ats_slug") or None,
        "ats_url": enriched.get("ats_url") or None,
        "careers_confidence": enriched.get("careers_confidence"),
        "identity_confidence": min(0.98, 0.88 + score * 0.1),
        "domain_evidence": [{
            "class": "official_site_identity", "url": str(response.url),
            "homepage_name_similarity": round(score, 4),
            "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }],
        "provenance": {"result": "domain_verified", "candidate_domain": domain,
                       "homepage_url": str(response.url),
                       "homepage_name_similarity": round(score, 4)},
    }


def verify_domains(*, limit: int = 2000, workers: int = 4,
                   min_interval: float = 0.2) -> dict:
    rows = master_db.list_domain_candidates(limit=limit)
    limiter = RequestLimiter(min_interval)
    verified = []
    errors = 0

    def work(row: dict):
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"User-Agent": USER_AGENT}) as client:
            return verify_record(row, client=client, limiter=limiter)

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 4))) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception:
                errors += 1
                continue
            if result:
                verified.append(result)
    updated = master_db.save_verified_domains(verified)
    return {"selected": len(rows), "verified": len(verified), "updated": updated,
            "unverified": len(rows) - len(verified), "errors": errors}


def discover_search_domains(*, limit: int = 100, workers: int = 4,
                            min_interval: float = 0.5) -> dict:
    """Discover official sites from search, then verify them against E-Verify identity."""
    rows = master_db.list_search_candidates(limit=limit)
    limiter = RequestLimiter(min_interval)
    verified = []
    attempts: list[tuple[int, dict]] = []
    errors = 0

    def work(row: dict):
        attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        record = {**row, "canonical_name": row.get("canonical_name")
                  or company_db.normalize_company_name(row.get("brand_name"))}
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"User-Agent": USER_AGENT}) as client:
            candidates = search_candidates(record, client, limiter, max_results=3)
            if not candidates:
                # Empty search HTML is ambiguous: no result, throttling and provider
                # markup changes are indistinguishable here, so keep it retryable.
                return (None, {"attempted_at": attempted_at,
                               "result": "transient_or_empty_search"})
            for candidate in candidates:
                accepted = verify_candidate(record, candidate, client, limiter)
                if not accepted:
                    continue
                domain = accepted["domain"]
                if not _country_compatible_domain(row, domain):
                    continue
                overlap = _meaningful_domain_overlap(row, domain)
                homepage_score = float(
                    accepted["evidence"].get("homepage_name_similarity") or 0
                )
                if overlap <= 0:
                    continue
                enriched = enrich_company(
                    {"domain": domain, "legal_name": row.get("legal_name"),
                     "trade_name": row.get("trade_name")}, client,
                    before_request=limiter.wait)
                return ({
                    "company_id": row["company_id"], "domain": domain,
                    "careers_url": enriched.get("careers_url") or None,
                    "ats": enriched.get("ats") or None,
                    "ats_slug": enriched.get("ats_slug") or None,
                    "ats_url": enriched.get("ats_url") or None,
                    "careers_confidence": enriched.get("careers_confidence"),
                    "identity_confidence": accepted["domain_confidence"],
                    "domain_evidence": [{
                        "class": "official_site_identity", "discovered_via": "public_search",
                        "url": accepted["candidate_url"],
                        "homepage_name_similarity": accepted["evidence"].get(
                            "homepage_name_similarity"),
                        "meaningful_domain_overlap": round(overlap, 4),
                        "verified_at": attempted_at,
                    }],
                    "provenance": {"result": "domain_verified", "method": "search+official_site",
                                   "candidate_url": accepted["candidate_url"],
                                   "homepage_name_similarity": accepted["evidence"].get(
                                       "homepage_name_similarity")},
                }, {"attempted_at": attempted_at, "result": "verified", "domain": domain})
        return (None, {"attempted_at": attempted_at, "result": "no_verified_domain"})

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 4))) as pool:
        futures = {pool.submit(work, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result, attempt = future.result()
            except Exception as exc:
                errors += 1
                attempt = {"attempted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "result": "transient_error", "error": str(exc)[:160]}
                result = None
            if result:
                verified.append(result)
            # Provider/network failures remain retryable; completed no-match attempts persist.
            if attempt.get("result") not in {"transient_error", "transient_or_empty_search"}:
                attempts.append((int(row["company_id"]), attempt))
    updated = master_db.save_verified_domains(verified)
    attempted = master_db.record_search_attempts(attempts)
    return {"selected": len(rows), "verified": len(verified), "updated": updated,
            "attempted": attempted, "errors": errors}


def audit_search_domains() -> dict:
    rows = master_db.list_all_verified_domains()
    rejected = []
    for row in rows:
        domain = row.get("domain") or ""
        normalized_domain = company_db.normalize_domain(domain)
        evidence = row.get("domain_evidence") or []
        has_structured_domain_link = any(
            item.get("class") == "structured_corporate_source"
            and company_db.normalize_domain(item.get("candidate_domain")) == normalized_domain
            for item in evidence if isinstance(item, dict)
        )
        has_official_site_identity = any(
            item.get("class") == "official_site_identity"
            for item in evidence if isinstance(item, dict)
        )
        if (not has_structured_domain_link or not has_official_site_identity
                or not _country_compatible_domain(row, domain)):
            rejected.append(int(row["company_id"]))
            continue
        if (any(item.get("discovered_via") == "public_search"
                for item in evidence if isinstance(item, dict))
                and _meaningful_domain_overlap(row, domain) <= 0):
            rejected.append(int(row["company_id"]))
    removed = master_db.quarantine_domain_ids(
        rejected, reason="domain_evidence_contract_failed")
    return {"checked": len(rows), "rejected": removed}
