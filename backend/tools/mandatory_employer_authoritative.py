"""Validated, reproducible enrichment for the 15 mandatory employers.

The fixture is deliberately hand-curated from issuer/regulator sources.  This
module never discovers companies and never treats search results as evidence.
"""
from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from backend.tools import company_discovery_db as company_db


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / \
    "mandatory_employers_authoritative.json"

# Stable identifiers used when the mandatory seed was first inserted.  They
# intentionally remain separate from the asserted current domain (TP rebranded).
LEGACY_SOURCE_IDS = {
    "amazon": "amazon.com",
    "concentrix": "concentrix.com",
    "foundever": "foundever.com",
    "ttec": "ttec.com",
    "teleperformance": "teleperformance.com",
    "cvs_health": "cvshealth.com",
    "unitedhealth_group": "unitedhealthgroup.com",
    "jpmorgan_chase": "jpmorganchase.com",
    "walmart": "walmart.com",
    "target": "target.com",
    "hilton": "hilton.com",
    "marriott": "marriott.com",
    "progressive": "progressive.com",
    "state_farm": "statefarm.com",
    "allstate": "allstate.com",
}

REQUIRED_FACTS = {
    "legal_name", "exact_domain", "employee_count", "industry",
    "operational_headquarters",
}
SEARCH_HOST_PARTS = ("google.", "bing.com", "duckduckgo.com", "search.yahoo.com")


def load_fixture(path: Path | str = FIXTURE_PATH) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_fixture(payload)
    return payload


def _valid_date(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    try:
        date.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def validate_fixture(payload: dict) -> None:
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported fixture schema_version")
    if not _valid_date(payload.get("observed_at")):
        raise ValueError("fixture observed_at must be an ISO date")
    rows = payload.get("employers")
    if not isinstance(rows, list) or len(rows) != 15:
        raise ValueError("fixture must contain exactly 15 employers")
    keys = [str(row.get("key") or "") for row in rows]
    domains = [company_db.normalize_domain(row.get("exact_domain")) for row in rows]
    if set(keys) != set(LEGACY_SOURCE_IDS) or len(set(keys)) != 15:
        raise ValueError("fixture keys must match the 15 mandatory employers")
    if any(not domain for domain in domains) or len(set(domains)) != 15:
        raise ValueError("exact domains must be non-empty and unique")

    for row, normalized_domain in zip(rows, domains):
        key = row["key"]
        for field in ("brand_name", "legal_name", "industry",
                      "employee_count_qualifier", "employee_count_scope"):
            if not str(row.get(field) or "").strip():
                raise ValueError(f"{key}: missing {field}")
        if normalized_domain != row["exact_domain"]:
            raise ValueError(f"{key}: exact_domain must already be normalized")
        count = row.get("employee_count")
        minimum = row.get("employee_count_min")
        maximum = row.get("employee_count_max")
        if count is None and minimum is None:
            raise ValueError(f"{key}: missing employee count or lower bound")
        for field, value in (("employee_count", count),
                             ("employee_count_min", minimum),
                             ("employee_count_max", maximum)):
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ValueError(f"{key}: invalid {field}")
        if count is not None and minimum is not None:
            raise ValueError(f"{key}: exact/approximate count and lower bound conflict")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"{key}: employee bounds conflict")
        if not _valid_date(row.get("employee_count_as_of"), nullable=True):
            raise ValueError(f"{key}: invalid employee_count_as_of")
        hq = row.get("operational_headquarters") or {}
        if not str(hq.get("city") or "").strip() or not str(hq.get("country") or "").strip():
            raise ValueError(f"{key}: operational headquarters needs city and country")

        sources = row.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"{key}: sources required")
        supported: set[str] = set()
        source_ids: set[str] = set()
        for source in sources:
            source_id = str(source.get("id") or "").strip()
            if not source_id or source_id in source_ids:
                raise ValueError(f"{key}: source ids must be non-empty and unique")
            source_ids.add(source_id)
            url = str(source.get("url") or "")
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"http", "https"} or not host:
                raise ValueError(f"{key}/{source_id}: invalid source URL")
            if any(part in host for part in SEARCH_HOST_PARTS):
                raise ValueError(f"{key}/{source_id}: search pages are not evidence")
            if not _valid_date(source.get("observed_at")):
                raise ValueError(f"{key}/{source_id}: observed_at required")
            if not str(source.get("publisher") or "").strip() or \
                    not str(source.get("type") or "").strip():
                raise ValueError(f"{key}/{source_id}: publisher and type required")
            supported.update(str(item) for item in source.get("supports") or [])
        missing = REQUIRED_FACTS - supported
        if missing:
            raise ValueError(f"{key}: unsupported facts: {sorted(missing)}")


def audit_records(records: list[dict]) -> dict:
    gaps = []
    caveats = []
    for row in records:
        if row.get("employee_count_as_of") is None:
            gaps.append({"key": row["key"], "field": "employee_count_as_of",
                         "reason": "official page publishes no as-of date"})
        if row.get("employee_count") is None:
            caveats.append({"key": row["key"], "field": "employee_count",
                            "reason": "source reports a lower bound",
                            "minimum": row.get("employee_count_min")})
        scope = str(row.get("employee_count_scope") or "")
        if "excludes" in scope or "managed" in scope:
            caveats.append({"key": row["key"], "field": "employee_count_scope",
                            "reason": scope})
    return {"data_gaps": gaps, "caveats": caveats}


def _employee_size(row: dict) -> str:
    if row.get("employee_count") is not None:
        return f'{row["employee_count_qualifier"]} {row["employee_count"]:,}'
    return f'{row["employee_count_qualifier"]} {row["employee_count_min"]:,}'


def _hq(row: dict) -> str:
    hq = row["operational_headquarters"]
    return ", ".join(part for part in (hq["city"], hq.get("region"), hq["country"])
                     if part)


def _evidence(row: dict, observed_at: str) -> dict:
    return {
        "provider": "mandatory_authoritative",
        "class": "authoritative_first_factor",
        "evidence_class": "authoritative_first_factor",
        "assertion": "reported_official_domain",
        "observed_at": observed_at,
        "legal_name": row["legal_name"],
        "brand_name": row["brand_name"],
        "brand_aliases": row["brand_aliases"],
        "exact_domain": row["exact_domain"],
        "employee_count": row.get("employee_count"),
        "employee_count_min": row.get("employee_count_min"),
        "employee_count_max": row.get("employee_count_max"),
        "employee_count_qualifier": row["employee_count_qualifier"],
        "employee_count_scope": row["employee_count_scope"],
        "employee_count_as_of": row.get("employee_count_as_of"),
        "industry": row["industry"],
        "operational_headquarters": row["operational_headquarters"],
        "sources": deepcopy(row["sources"]),
    }


def apply_fixture(payload: dict, *, connection_factory=company_db.conn) -> dict:
    """Atomically update only the existing mandatory seed identities."""
    validate_fixture(payload)
    records = payload["employers"]
    external_ids = [LEGACY_SOURCE_IDS[row["key"]] for row in records]
    with connection_factory() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT id,source_external_id FROM company_discovery "
                "WHERE source='mandatory_employer' AND source_external_id=ANY(%s) "
                "ORDER BY source_external_id", (external_ids,))
            found = cursor.fetchall()
            by_external = {str(row[1]): int(row[0]) for row in found}
            if len(found) != 15 or set(by_external) != set(external_ids):
                missing = sorted(set(external_ids) - set(by_external))
                raise RuntimeError(f"mandatory seed preflight failed; missing={missing}")

            for row in records:
                external_id = LEGACY_SOURCE_IDS[row["key"]]
                company_id = by_external[external_id]
                evidence = _evidence(row, payload["observed_at"])
                provenance = json.dumps({"mandatory_authoritative": evidence})
                metadata = json.dumps({
                    "mandatory_authoritative": True,
                    "brand_name": row["brand_name"],
                    "brand_aliases": row["brand_aliases"],
                    "operational_headquarters": row["operational_headquarters"],
                })
                cursor.execute("""
                    UPDATE company_discovery SET
                      legal_name=%s,trade_name=%s,canonical_name=%s,domain=%s,
                      country=%s,industry=%s,employee_size=%s,
                      domain_confidence=1.0,
                      provenance=provenance || %s::jsonb,
                      metadata=metadata || %s::jsonb,updated_at=now()
                    WHERE id=%s AND source='mandatory_employer'
                """, (row["legal_name"], row["brand_name"],
                      company_db.normalize_company_name(row["brand_name"]),
                      row["exact_domain"],
                      row["operational_headquarters"]["country"], row["industry"],
                      _employee_size(row), provenance, metadata, company_id))

                first_source = row["sources"][0]
                size_source = ("mandatory_authoritative:"
                               f'{first_source["id"]}:{row.get("employee_count_as_of") or "undated"}')
                brand_identity = json.dumps({"mandatory_authoritative": {
                    "legal_name": row["legal_name"], "brand_name": row["brand_name"],
                    "aliases": row["brand_aliases"], "sources": row["sources"],
                }})
                domain_evidence = json.dumps([{
                    "provider": "mandatory_authoritative",
                    "class": "authoritative_first_factor",
                    "evidence_class": "authoritative_first_factor",
                    "assertion": "reported_official_domain",
                    "domain": row["exact_domain"], "confidence": 1.0,
                    "observed_at": payload["observed_at"],
                    "sources": [source for source in row["sources"]
                                if "exact_domain" in source["supports"]],
                }])
                qualification = json.dumps({"mandatory_authoritative": evidence})
                cursor.execute("""
                    UPDATE company_employer_master SET
                      brand_name=%s,employee_count=%s,employee_count_min=%s,
                      employee_count_max=%s,employee_size_source=%s,industry=%s,
                      headquarters=%s,headquarters_country=%s,candidate_domain=%s,
                      domain_verified=EXISTS (
                        SELECT 1 FROM jsonb_array_elements(domain_evidence) item
                        WHERE item->>'provider'='official_site_identity'
                          AND item->>'domain'=%s
                      ),
                      identity_confidence=CASE WHEN EXISTS (
                        SELECT 1 FROM jsonb_array_elements(domain_evidence) item
                        WHERE item->>'provider'='official_site_identity'
                          AND item->>'domain'=%s
                      ) THEN GREATEST(identity_confidence,0.99)
                        ELSE LEAST(identity_confidence,0.75) END,
                      brand_identity=brand_identity || %s::jsonb,
                      domain_evidence=COALESCE((
                        SELECT jsonb_agg(item) FROM jsonb_array_elements(domain_evidence) item
                        WHERE item->>'provider' <> 'mandatory_authoritative'
                      ),'[]'::jsonb) || %s::jsonb,
                      qualification_evidence=qualification_evidence || %s::jsonb,
                      last_verified_at=now(),updated_at=now()
                    WHERE company_id=%s AND in_target_population
                """, (row["brand_name"], row.get("employee_count"),
                      row.get("employee_count_min"), row.get("employee_count_max"),
                      size_source, row["industry"], _hq(row),
                      row["operational_headquarters"]["country"], row["exact_domain"],
                      row["exact_domain"], row["exact_domain"],
                      brand_identity, domain_evidence, qualification, company_id))
                if cursor.rowcount != 1:
                    raise RuntimeError(f'{row["key"]}: employer master row missing')
        finally:
            cursor.close()
    return {"validated": len(records), "updated": len(records),
            **audit_records(records)}


def report(payload: dict) -> dict:
    validate_fixture(payload)
    records = payload["employers"]
    return {"validated": len(records), "domains": len({r["exact_domain"] for r in records}),
            "dated_employee_counts": sum(r.get("employee_count_as_of") is not None
                                         for r in records),
            **audit_records(records)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "apply"), nargs="?",
                        default="preview")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    args = parser.parse_args(argv)
    payload = load_fixture(args.fixture)
    result = apply_fixture(payload) if args.command == "apply" else report(payload)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
