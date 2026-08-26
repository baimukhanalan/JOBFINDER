"""Bounded independent official-site verification for mandatory employers."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools.company_enrichment import _get, canonical_domain, official_url
from backend.tools.employer_fdic_domain_verification import verify_identity_page


FIRST_PROVIDER = "mandatory_authoritative_domain_assertion"
SECOND_PROVIDER = "official_site_identity"


def _authoritative_assertion(row: Mapping[str, Any]) -> dict | None:
    domain = canonical_domain(row.get("domain"))
    for item in row.get("domain_evidence") or []:
        if (item.get("provider") == "mandatory_authoritative"
                and item.get("class") == "authoritative_first_factor"
                and item.get("assertion") == "reported_official_domain"
                and canonical_domain(item.get("domain")) == domain):
            return dict(item)
    return None


def _identity_names(row: Mapping[str, Any]) -> tuple[str, str, str]:
    identity = row.get("brand_identity") or {}
    mandatory = identity.get("mandatory_authoritative") or {}
    aliases = mandatory.get("aliases") or []
    trade_name = next((str(value) for value in aliases if str(value).strip()), "")
    return (str(row.get("legal_name") or ""), str(row.get("brand_name") or ""),
            trade_name)


def apply_passes(passes: list[Mapping[str, Any]]) -> dict:
    if not passes:
        return {"selected": 0, "updated": 0}
    by_id = {int(item["company_id"]): item for item in passes}
    if len(by_id) != len(passes):
        raise ValueError("duplicate mandatory company_id")
    ids = sorted(by_id)
    with company_db._cur(False) as cur:
        cur.execute("""
          SELECT c.id,c.domain,m.domain_verified,m.domain_evidence
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE c.source='mandatory_employer' AND m.mandatory_seed
            AND m.in_target_population AND c.id=ANY(%s) FOR UPDATE
        """, (ids,))
        locked = {int(row[0]): {"domain": canonical_domain(row[1]),
                                "verified": bool(row[2]), "evidence": row[3] or []}
                  for row in cur.fetchall()}
        if set(locked) != set(ids):
            raise RuntimeError("could not lock every mandatory verification pass")
        for company_id in ids:
            item = by_id[company_id]
            domain = canonical_domain(item["domain"])
            identity = item["identity"]
            first = item["first_factor"]
            if locked[company_id]["domain"] != domain:
                raise RuntimeError("mandatory domain changed before verification apply")
            existing = {"domain": domain, "domain_evidence": locked[company_id]["evidence"]}
            if not _authoritative_assertion(existing):
                raise RuntimeError("mandatory authoritative assertion missing at apply")
            if (canonical_domain(first.get("domain")) != domain
                    or first.get("assertion") != "reported_official_domain"):
                raise RuntimeError("invalid mandatory first factor")
            if not identity.get("passed") or identity.get("proposed_domain") != domain:
                raise RuntimeError("only exact official-site identity passes may be applied")
            observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            structured_first = {
                "provider": FIRST_PROVIDER, "class": "structured_corporate_source",
                "assertion": "reported_official_domain", "candidate_domain": domain,
                "source_provider": "mandatory_authoritative",
                "sources": first.get("sources") or [],
                "observed_at": first.get("observed_at"),
            }
            second = {
                "provider": SECOND_PROVIDER, "class": "official_site_identity",
                "assertion": "exact_employer_identity_in_trusted_page_context",
                "domain": domain, "homepage_url": identity["final_url"],
                "matched_name": identity["matched_name"],
                "context_type": identity["context_type"],
                "context_excerpt": identity["context_excerpt"],
                "observed_at": observed_at,
            }
            encoded = json.dumps([structured_first, second])
            qualification = json.dumps({"mandatory_domain_verification": {
                "status": "passed", "first_factor": structured_first,
                "second_factor": second}})
            cur.execute("""
              UPDATE company_employer_master SET candidate_domain=%s,
                domain_verified=TRUE,identity_confidence=GREATEST(identity_confidence,0.99),
                domain_evidence=COALESCE((SELECT jsonb_agg(e)
                  FROM jsonb_array_elements(domain_evidence) e
                  WHERE NOT (e->>'provider' IN (%s,%s))), '[]'::jsonb) || %s::jsonb,
                qualification_evidence=qualification_evidence || %s::jsonb,
                last_verified_at=now(),updated_at=now()
              WHERE company_id=%s AND mandatory_seed AND in_target_population
            """, (domain, FIRST_PROVIDER, SECOND_PROVIDER, encoded,
                  qualification, company_id))
            if cur.rowcount != 1:
                raise RuntimeError("mandatory domain verification update failed")
    return {"selected": len(passes), "updated": len(passes)}


def verify_unverified_mandatory(*, limit: int = 15, min_interval: float = 0.15,
                                client: httpx.Client | None = None) -> dict:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.legal_name,c.domain,m.brand_name,m.brand_identity,m.domain_evidence
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE c.source='mandatory_employer' AND m.mandatory_seed
            AND m.in_target_population AND NOT m.domain_verified
          ORDER BY c.id LIMIT %s
        """, (max(1, min(int(limit), 15)),))
        rows = [dict(row) for row in cur.fetchall()]
    owned = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(15.0), headers={
            "User-Agent": "JobFinder-mandatory-identity-verification/1.0"})
    passes, failures = [], []
    try:
        for index, row in enumerate(rows):
            try:
                first = _authoritative_assertion(row)
                if first is None:
                    failures.append({"company_id": row["id"],
                                     "reason": "authoritative_domain_assertion_missing"})
                    continue
                domain = canonical_domain(row["domain"])
                response = _get(client, official_url(domain), retries=1)
                if response is None:
                    failures.append({"company_id": row["id"], "domain": domain,
                                     "reason": "official_site_unavailable"})
                    continue
                legal, brand, trade = _identity_names(row)
                identity = verify_identity_page(
                    proposed_domain=domain, final_url=str(response.url),
                    page_html=response.text, legal_name=legal, brand_name=brand,
                    trade_name=trade)
                if not identity["passed"]:
                    failures.append({"company_id": row["id"], "domain": domain,
                                     "reason": identity["reason"], "identity": identity})
                    continue
                passes.append({"company_id": row["id"], "domain": domain,
                               "first_factor": first, "identity": identity})
            except Exception as exc:
                failures.append({"company_id": row["id"],
                                 "reason": "bounded_verification_error",
                                 "error": str(exc)})
            finally:
                if min_interval > 0 and index + 1 < len(rows):
                    time.sleep(min_interval)
    finally:
        if owned:
            client.close()
    applied = apply_passes(passes)
    return {"selected": len(rows), "passed": len(passes), "failed": len(failures),
            "apply_selected": applied["selected"], "updated": applied["updated"],
            "passes": passes, "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--min-interval", type=float, default=0.15)
    args = parser.parse_args(argv)
    print(json.dumps(verify_unverified_mandatory(
        limit=args.limit, min_interval=args.min_interval), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
