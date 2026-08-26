"""Secret-safe owner configuration preflight for official enrichment sources."""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Mapping

from backend.tools import company_discovery_db as company_db


PLACEHOLDERS = re.compile(r"^(?:change[-_ ]?me|replace[-_ ]?me|example|test|todo|xxx+)$", re.I)
EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def _sam_status(environ: Mapping[str, str]) -> dict:
    value = str(environ.get("SAM_API_KEY") or "")
    present = bool(value.strip())
    valid = bool(present and 16 <= len(value) <= 256
                 and not any(char.isspace() or ord(char) < 32 for char in value)
                 and not PLACEHOLDERS.fullmatch(value.strip()))
    return {"present": present, "format_valid": valid,
            "format_requirement": "16-256 non-whitespace characters; not a placeholder"}


def _sec_status(environ: Mapping[str, str]) -> dict:
    value = str(environ.get("SEC_USER_AGENT") or "")
    present = bool(value.strip())
    tokens = value.strip().split()
    has_identity = any(not EMAIL.fullmatch(token.strip("(),;<>")) for token in tokens)
    has_email = any(EMAIL.fullmatch(token.strip("(),;<>")) for token in tokens)
    valid = bool(present and 8 <= len(value) <= 256 and "\n" not in value and "\r" not in value
                 and has_identity and has_email and not PLACEHOLDERS.fullmatch(value.strip()))
    return {"present": present, "format_valid": valid,
            "format_requirement": "organization or application identity plus contact email; one line"}


def affected_active_counts() -> dict:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT COUNT(*) AS active_total,
            COUNT(*) FILTER (WHERE NULLIF(c.external_ids->>'sam_uei','') IS NOT NULL)
              AS sam_linked_active,
            COUNT(*) FILTER (WHERE NULLIF(c.external_ids->>'sam_uei','') IS NOT NULL
              AND (NULLIF(m.naics_code,'') IS NULL OR NULLIF(m.industry,'') IS NULL
                OR NULLIF(m.headquarters,'') IS NULL)) AS sam_linked_with_gaps,
            COUNT(*) FILTER (WHERE NULLIF(c.external_ids->>'sec_cik','') IS NOT NULL)
              AS sec_linked_active,
            COUNT(*) FILTER (WHERE NULLIF(c.external_ids->>'sec_cik','') IS NOT NULL
              AND (m.employee_count IS NULL OR NULLIF(m.industry,'') IS NULL
                OR NULLIF(m.headquarters,'') IS NULL)) AS sec_linked_with_gaps,
            COUNT(*) FILTER (WHERE NULLIF(c.external_ids->>'irs_ein','') IS NOT NULL)
              AS irs_linked_active,
            COUNT(*) FILTER (WHERE NULLIF(c.external_ids->>'fdic_cert','') IS NOT NULL)
              AS fdic_linked_active
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population
        """)
        return {key: int(value) for key, value in dict(cur.fetchone()).items()}


def build_report(*, environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    counts = affected_active_counts()
    sam = _sam_status(env)
    sec = _sec_status(env)
    sam.update({"linked_active": counts["sam_linked_active"],
                "linked_with_gaps": counts["sam_linked_with_gaps"],
                "ready": sam["format_valid"]})
    sec.update({"linked_active": counts["sec_linked_active"],
                "linked_with_gaps": counts["sec_linked_with_gaps"],
                "ready": sec["format_valid"]})
    return {
        "schema_version": 1,
        "active_total": counts["active_total"],
        "owner_config": {"SAM_API_KEY": sam, "SEC_USER_AGENT": sec},
        "no_key_sources": {
            "FDIC": {"requires_key": False,
                     "linked_active": counts["fdic_linked_active"]},
            "IRS_990": {"requires_key": False,
                        "linked_active": counts["irs_linked_active"]},
        },
        "resume_commands": {
            "preflight": ".venv/bin/python -m backend.tools.employer_official_config_preflight --require-ready",
            "sam_exact_uei": ".venv/bin/python -m backend.tools.employer_official_enrichment sam --uei '<12_CHARACTER_UEI>'",
            "sec_exact_cik": ".venv/bin/python -m backend.tools.employer_official_enrichment sec --cik '<CIK>'",
            "fdic_exact_cert": ".venv/bin/python -m backend.tools.employer_official_enrichment fdic --cert '<FDIC_CERT>'",
            "fdic_linked_batch": ".venv/bin/python -m backend.tools.employer_official_enrichment fdic-linked --limit 50 --min-interval .1",
            "irs_exact_filing": ".venv/bin/python -m backend.tools.employer_official_enrichment irs-xml --ein '<EIN>' --url '<OFFICIAL_IRS_XML_URL>'",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-ready", action="store_true",
                        help="exit 2 unless both owner-provided settings have valid format")
    args = parser.parse_args(argv)
    report = build_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_ready and not all(
            item["ready"] for item in report["owner_config"].values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
