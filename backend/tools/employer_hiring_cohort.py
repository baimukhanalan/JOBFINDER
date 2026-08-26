"""Strict, auditable promotion from employer reservoir to verified hiring cohort.

The active 10k is a candidate reservoir, not a list of application-ready employers.
This module evaluates current evidence without fetching jobs or touching the queue.
Its CLI is dry-run by default; ``--apply`` only persists cohort status/evidence.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from backend.tools import company_discovery_db as company_db
from backend.tools.company_job_sources import SUPPORTED_ATS

try:
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover
    Json = None


COHORT_STATES = (
    "reservoir_candidate", "evidence_incomplete", "verified_hiring", "quarantined",
)
HARD_IDENTITY_RISKS = {
    "shell_or_shared_services", "fund_or_trust", "aggregate_or_sentence_name",
}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _evidence_domain(item: Mapping[str, Any]) -> str:
    for key in ("candidate_domain", "domain", "homepage_url", "url"):
        domain = company_db.normalize_domain(item.get(key))
        if domain:
            return domain
    return ""


def _valid_url(value: Any) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def evaluate_hiring_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic decision; never infer readiness from reservoir rank."""
    company_id = int(row.get("company_id") or row.get("id") or 0)
    domain = company_db.normalize_domain(row.get("domain"))
    evidence = _as_list(row.get("domain_evidence"))
    first_factors = [item for item in evidence
                     if item.get("class") == "structured_corporate_source"
                     and _evidence_domain(item) == domain]
    second_factors = [item for item in evidence
                      if item.get("class") == "official_site_identity"
                      and _evidence_domain(item) == domain]
    ats = str(row.get("ats") or "").strip().casefold()
    ats_slug = str(row.get("ats_slug") or "").strip()
    complete_scans = int(row.get("authoritative_complete_scans") or 0)
    active_jobs = int(row.get("active_authoritative_jobs") or 0)
    risks = {str(value) for value in (row.get("entity_risk_flags") or [])}

    checks = {
        "identity_verified": row.get("identity_status") == "verified",
        "domain_verified": bool(row.get("domain_verified")) and bool(domain),
        "structured_domain_factor": bool(first_factors),
        "official_site_domain_factor": bool(second_factors),
        "careers_url": _valid_url(row.get("careers_url")),
        "supported_ats": ats in SUPPORTED_ATS,
        "ats_slug": bool(ats_slug),
        "authoritative_scan_complete": complete_scans >= 1,
        "active_job_observed": active_jobs >= 1,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    quarantined = row.get("identity_status") in {"quarantined", "rejected"} or bool(
        risks & HARD_IDENTITY_RISKS)
    if quarantined:
        status = "quarantined"
    elif not blockers:
        status = "verified_hiring"
    else:
        status = "evidence_incomplete"
    return {
        "company_id": company_id,
        "stored_status": str(row.get("hiring_cohort_status") or "reservoir_candidate"),
        "status": status,
        "eligible": status == "verified_hiring",
        "blockers": blockers,
        "checks": checks,
        "evidence": {
            "domain": domain,
            "domain_factor_count": len(first_factors) + len(second_factors),
            "structured_factor_count": len(first_factors),
            "official_site_factor_count": len(second_factors),
            "careers_url": str(row.get("careers_url") or ""),
            "ats": ats,
            "ats_slug": ats_slug,
            "authoritative_complete_scans": complete_scans,
            "active_authoritative_jobs": active_jobs,
        },
    }


_CONTRACT_SELECT = """
  SELECT m.company_id,m.identity_status,m.domain_verified,m.domain_evidence,
    m.entity_risk_flags,m.hiring_cohort_status,c.domain,c.careers_url,c.ats,c.ats_slug,
    COALESCE(s.authoritative_complete_scans,0) authoritative_complete_scans,
    COALESCE(j.active_authoritative_jobs,0) active_authoritative_jobs
  FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
  LEFT JOIN LATERAL (
    SELECT COUNT(*) authoritative_complete_scans
    FROM company_remote_job_scans scan
    WHERE scan.company_id=m.company_id AND scan.scan_complete AND scan.scan_succeeded
      AND lower(BTRIM(scan.source))=lower(BTRIM(c.ats))
      AND lower(BTRIM(scan.source_board_id))=lower(BTRIM(c.ats_slug))
  ) s ON TRUE
  LEFT JOIN LATERAL (
    SELECT COUNT(*) active_authoritative_jobs
    FROM company_remote_jobs job JOIN company_remote_job_scans scan
      ON scan.id=job.last_scan_id AND scan.company_id=job.company_id
    WHERE job.company_id=m.company_id AND job.status='active'
      AND scan.scan_complete AND scan.scan_succeeded
      AND lower(BTRIM(job.source))=lower(BTRIM(c.ats))
      AND lower(BTRIM(job.source_board_id))=lower(BTRIM(c.ats_slug))
      AND lower(BTRIM(scan.source))=lower(BTRIM(c.ats))
      AND lower(BTRIM(scan.source_board_id))=lower(BTRIM(c.ats_slug))
  ) j ON TRUE
  WHERE m.in_target_population
  ORDER BY m.company_id
"""


def _summary(decisions: list[dict[str, Any]], *, applied: bool,
             include_decisions: bool = False) -> dict[str, Any]:
    statuses = Counter(item["status"] for item in decisions)
    blockers = Counter(blocker for item in decisions for blocker in item["blockers"])
    result = {
        "selected": len(decisions), "applied": applied,
        "verified_hiring": statuses["verified_hiring"],
        "evidence_incomplete": statuses["evidence_incomplete"],
        "quarantined": statuses["quarantined"],
        "blockers": dict(sorted(blockers.items())),
    }
    if include_decisions:
        result["decisions"] = decisions
    return result


def refresh_hiring_cohort(*, limit: int = 10_000, apply: bool = False,
                          include_decisions: bool = False) -> dict[str, Any]:
    """Evaluate a bounded cohort; persist only when explicitly requested."""
    if limit < 1:
        raise ValueError("limit must be positive")
    with company_db._cur() as cur:
        lock = " FOR UPDATE OF m" if apply else ""
        cur.execute(f"{_CONTRACT_SELECT} LIMIT %s{lock}", (int(limit),))
        rows = [dict(row) for row in cur.fetchall()]
        decisions = [evaluate_hiring_contract(row) for row in rows]
        if apply:
            evaluated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for decision in decisions:
                audit = {**decision, "contract": "verified_hiring_v1",
                         "evaluated_at": evaluated_at}
                encoded = Json(audit) if Json is not None else audit
                cur.execute("""
                  UPDATE company_employer_master SET hiring_cohort_status=%s,
                    hiring_cohort_evidence=%s,hiring_cohort_checked_at=now(),
                    monitoring_status=CASE
                      WHEN %s='verified_hiring' AND is_monitoring_representative
                        AND monitoring_status='candidate' THEN 'qualified'
                      WHEN %s<>'verified_hiring' AND monitoring_status IN ('qualified','monitoring')
                        THEN 'candidate'
                      ELSE monitoring_status END,updated_at=now()
                  WHERE company_id=%s AND in_target_population
                """, (decision["status"], encoded, decision["status"],
                      decision["status"], decision["company_id"]))
                if cur.rowcount != 1:
                    raise RuntimeError("hiring cohort changed during qualification")
    return _summary(decisions, applied=apply, include_decisions=include_decisions)


def audit_hiring_cohort(*, limit: int = 10_000) -> dict[str, Any]:
    result = refresh_hiring_cohort(limit=limit, apply=False, include_decisions=True)
    decisions = result.pop("decisions")
    stored_mismatch = sum(decision["status"] != decision["stored_status"]
                          for decision in decisions)
    mismatch_ids = [decision["company_id"] for decision in decisions
                    if decision["status"] != decision["stored_status"]]
    return {**result, "stored_mismatch": stored_mismatch,
            "stored_mismatch_company_ids": mismatch_ids[:100]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--apply", action="store_true",
                        help="persist cohort decisions; default is read-only dry-run")
    args = parser.parse_args(argv)
    result = refresh_hiring_cohort(limit=args.limit, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
