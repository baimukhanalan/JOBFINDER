"""Deterministic employer lanes and entity-risk classification."""
from __future__ import annotations

import json
import re

from backend.tools import company_discovery_db as company_db


def classify_employer(record: dict) -> tuple[str, list[str]]:
    name = str(record.get("brand_name") or record.get("trade_name")
               or record.get("legal_name") or "").strip()
    industry = str(record.get("industry") or "").strip()
    blob = f"{name} {industry}".casefold()
    if re.search(r"\b(staffing|recruit(?:ing|ment)?|personnel|talent solutions)\b", blob):
        segment = "staffing"
    elif re.search(r"\b(government|department|county|city of|state of|federal|municipal)\b", blob):
        segment = "government"
    elif re.search(r"\b(university|college|school district|higher education|education)\b", blob):
        segment = "education"
    elif re.search(r"\b(health|hospital|medical|clinic|pharma|biotech)\b", blob):
        segment = "healthcare"
    elif re.search(r"\b(nonprofit|non-profit|foundation|charit(?:y|able))\b", blob):
        segment = "nonprofit"
    else:
        segment = "general"

    risks: list[str] = []
    if re.search(r"\b(payroll|shared services|management company|management services)\b", blob):
        risks.append("shell_or_shared_services")
    if re.search(r"\b(fund|trust|investment vehicle)\b", blob):
        risks.append("fund_or_trust")
    if re.search(r"\b(subsidiary|division|operating company|operations|systems)\b", blob):
        risks.append("affiliate_or_division")
    if len(name) > 80 or len(name.split()) > 12:
        risks.append("aggregate_or_sentence_name")
    return segment, list(dict.fromkeys(risks))


def refresh_segments() -> dict[str, int]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.brand_name,m.industry,c.legal_name,c.trade_name,
                 c.source,c.source_external_id
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population
          ORDER BY m.company_id
        """)
        rows = [dict(row) for row in cur.fetchall()]
    values = []
    counts: dict[str, int] = {}
    risky = 0
    for row in rows:
        segment, risks = classify_employer(row)
        counts[segment] = counts.get(segment, 0) + 1
        risky += int(bool(risks))
        brand_identity = {
            "brand_name": row.get("brand_name"), "legal_name": row.get("legal_name"),
            "trade_name": row.get("trade_name"), "source": row.get("source"),
            "source_external_id": row.get("source_external_id"),
        }
        values.append((segment, risks, brand_identity, int(row["company_id"])))
    with company_db._cur(False) as cur:
        cur.executemany("""
          UPDATE company_employer_master SET employer_segment=%s,
            entity_risk_flags=%s::jsonb,brand_identity=%s::jsonb,updated_at=now()
          WHERE company_id=%s AND in_target_population
        """, [(segment, json.dumps(risks), json.dumps(identity), company_id)
               for segment, risks, identity, company_id in values])
        updated = cur.rowcount
    return {"updated": updated, "risky": risky, **counts}
