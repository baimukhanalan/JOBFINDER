"""Read-only reproducible identity gap manifest for the active employer population."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.tools import company_discovery_db as company_db

CORE_FIELDS = ("legal_name", "brand_name", "employee_size", "industry", "naics",
               "headquarters", "employer_segment")
MANIFEST_FIELDS = ("company_id", "source", "source_external_id", "field", "reason",
                   "blocker", "resolution_class", "external_requirements", "retryable")


def _configured(name: str) -> bool:
    if str(os.getenv(name) or "").strip():
        return True
    env_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        return any(line.split("=", 1)[0].strip() == name and line.split("=", 1)[1].strip()
                   for line in env_path.read_text().splitlines()
                   if "=" in line and not line.lstrip().startswith("#"))
    except OSError:
        return False


def _values(row: dict) -> dict[str, bool]:
    metadata = dict(row.get("metadata") or {})
    snapshot = dict(metadata.get("source_snapshot") or {})
    return {
        "legal_name": bool(str(row.get("legal_name") or "").strip()),
        "brand_name": bool(str((row.get("brand_identity") or {}).get("brand_name") or "").strip()),
        "employee_size": row.get("employee_count") is not None
                         or row.get("employee_count_min") is not None,
        "industry": bool(str(row.get("industry") or "").strip()),
        "naics": bool(str(row.get("naics_code") or "").strip()),
        "headquarters": bool(str(row.get("headquarters") or "").strip()
                             and str(row.get("headquarters_address_type") or "").strip()),
        "employer_segment": bool(str(row.get("employer_segment") or "").strip()),
        "recipient_location": bool(((snapshot.get("recipient_location") or {}).get("value"))),
        "recipient_business_types": bool(snapshot.get("business_types")),
        "source_snapshot": (metadata.get("source_snapshot_refresh") or {}).get("status") == "success",
    }


def _classification(row: dict, field: str, reason: str) -> tuple[str, str, list[str], bool]:
    source = str(row["source"])
    if field == "source_snapshot":
        status = str(((row.get("metadata") or {}).get("source_snapshot_refresh") or {}).get("status") or "unattempted")
        if status == "error":
            return "official_source_network_error", "retry_later", ["source_available"], True
        if status == "pending":
            return "authoritative_id_binding_unresolved", "retry_later", ["source_available"], True
        return "official_snapshot_not_attempted", "retry_later", ["source_available"], True
    if field == "recipient_business_types":
        return ("award_search_does_not_expose_business_types", "owner_credential",
                ["SAM_API_KEY"], True)
    if field == "recipient_location":
        return "official_recipient_location_unresolved", "retry_later", ["source_available"], True
    if field in {"industry", "naics"} and source == "usaspending":
        return "sam_entity_classification_requires_access", "owner_credential", ["SAM_API_KEY"], True
    if field == "brand_name" and source == "usaspending":
        return "sam_dba_name_requires_access", "owner_credential", ["SAM_API_KEY"], True
    if field in {"industry", "naics", "employee_size"} and source == "gleif_lei":
        return ("gleif_does_not_publish_field", "authoritative_crosswalk",
                ["authoritative_ID_crosswalk"], True)
    if field == "brand_name" and source == "gleif_lei":
        return "gleif_record_has_no_other_name", "source_unavailable", ["source_available"], True
    if field == "headquarters" and source == "usaspending":
        return ("recipient_location_is_not_operational_headquarters", "source_unavailable",
                ["authoritative_ID_crosswalk"], True)
    if field in {"industry", "naics"} and source == "everify_large_employer":
        return ("everify_does_not_publish_field", "owner_credential",
                ["SAM_API_KEY", "authoritative_ID_crosswalk"], True)
    if field == "headquarters" and source == "everify_large_employer":
        return "everify_states_are_not_headquarters", "retry_later", ["source_available"], True
    if field in {"industry", "headquarters"} and source == "wikidata_employer":
        return "wikidata_statement_missing", "retry_later", ["source_available"], True
    if field == "naics" and source == "wikidata_employer":
        return ("wikidata_source_has_no_authoritative_naics", "owner_credential",
                ["SAM_API_KEY", "authoritative_ID_crosswalk"], True)
    if field == "employee_size" and source == "usaspending":
        return ("usaspending_does_not_publish_employee_size", "authoritative_crosswalk",
                ["authoritative_ID_crosswalk", "SEC_USER_AGENT"], True)
    if field == "naics" and source == "mandatory_employer":
        return ("mandatory_fixture_has_no_authoritative_naics", "owner_credential",
                ["SAM_API_KEY", "authoritative_ID_crosswalk"], True)
    return "stored_source_field_missing", "retry_later", ["source_available"], True


def build_report(*, include_manifest: bool = True) -> dict:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT m.company_id,m.brand_identity,m.employee_count,m.employee_count_min,
            m.industry,m.naics_code,m.headquarters,m.headquarters_address_type,
            m.employer_segment,m.identity_enrichment_gaps,
            c.source,c.source_external_id,c.legal_name,c.metadata
          FROM company_employer_master m JOIN company_discovery c ON c.id=m.company_id
          WHERE m.in_target_population ORDER BY c.source,c.source_external_id,m.company_id
        """)
        rows = [dict(row) for row in cur.fetchall()]
    fingerprint = hashlib.sha256("\n".join(
        f'{row["source"]}:{row["source_external_id"]}' for row in rows).encode()).hexdigest()
    coverage: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    manifest = []
    for row in rows:
        values = _values(row)
        applicable = list(CORE_FIELDS)
        if row["source"] in {"gleif_lei", "usaspending"}:
            applicable.append("source_snapshot")
        if row["source"] == "usaspending":
            applicable.extend(("recipient_location", "recipient_business_types"))
        stored_gaps = dict(row.get("identity_enrichment_gaps") or {})
        refresh = dict((row.get("metadata") or {}).get("source_snapshot_refresh") or {})
        snapshot = dict((row.get("metadata") or {}).get("source_snapshot") or {})
        for field in applicable:
            buckets = [coverage[row["source"]][field], coverage["__all__"][field]]
            for bucket in buckets:
                bucket["denominator"] += 1
            if values[field]:
                for bucket in buckets:
                    bucket["covered"] += 1
                continue
            for bucket in buckets:
                bucket["gap"] += 1
            if field == "source_snapshot":
                reason = str(refresh.get("error") or refresh.get("status") or "unattempted")
            elif field == "recipient_business_types":
                reason = str(snapshot.get("business_types_gap") or
                             "official_snapshot_has_no_business_types")
            elif field == "recipient_location":
                reason = "official_snapshot_has_no_recipient_location"
            else:
                reason = str(stored_gaps.get(field) or "stored_source_field_missing")
            blocker, resolution, requirements, retryable = _classification(row, field, reason)
            manifest.append({
                "company_id": int(row["company_id"]), "source": row["source"],
                "source_external_id": row["source_external_id"], "field": field,
                "reason": reason, "blocker": blocker, "resolution_class": resolution,
                "external_requirements": requirements, "retryable": retryable,
            })
    coverage_rows = []
    for source in sorted(coverage):
        for field in sorted(coverage[source]):
            counts = coverage[source][field]
            coverage_rows.append({"source": source, "field": field,
                                  "denominator": counts["denominator"],
                                  "covered": counts["covered"], "gap": counts["gap"],
                                  "coverage_rate": round(counts["covered"] / counts["denominator"], 6)})
    resolution_counts = Counter(row["resolution_class"] for row in manifest)
    requirement_counts = Counter(requirement for row in manifest
                                 for requirement in row["external_requirements"])
    credential_gap_count = sum(bool({"SAM_API_KEY", "SEC_USER_AGENT"}
                                    & set(row["external_requirements"])) for row in manifest)
    external = {
        "SAM_API_KEY": {"configured": _configured("SAM_API_KEY"),
                        "owner_credential": True,
                        "blocked_gaps": requirement_counts["SAM_API_KEY"]},
        "SEC_USER_AGENT": {"configured": _configured("SEC_USER_AGENT"),
                           "owner_credential": True,
                           "blocked_gaps": requirement_counts["SEC_USER_AGENT"]},
        "authoritative_ID_crosswalk": {"configured": False, "owner_credential": False,
                                       "blocked_gaps": requirement_counts["authoritative_ID_crosswalk"]},
        "source_available": {"configured": None, "owner_credential": False,
                             "blocked_gaps": requirement_counts["source_available"]},
    }
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active_population": len(rows), "population_fingerprint": fingerprint,
        "manifest_rows": len(manifest), "coverage": coverage_rows,
        "resolution_classes": dict(sorted(resolution_counts.items())),
        "external_requirements": external,
        "owner_credentials_required": [key for key, value in external.items()
                                       if value["owner_credential"] and not value["configured"]
                                       and value["blocked_gaps"]],
        "gaps_requiring_owner_credentials": credential_gap_count,
        "retry_without_owner_credentials": sum(
            count for key, count in resolution_counts.items()
            if key in {"retry_later", "source_unavailable", "authoritative_crosswalk"}),
    }
    if include_manifest:
        report["manifest"] = manifest
    return report


def write_csv(path: Path, manifest: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in manifest:
            writer.writerow({**row, "external_requirements": "|".join(row["external_requirements"]),
                             "retryable": str(row["retryable"]).lower()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--include-manifest", action="store_true")
    args = parser.parse_args(argv)
    full = build_report(include_manifest=True)
    if args.json:
        args.json.write_text(json.dumps(full, ensure_ascii=False, indent=2, default=str) + "\n")
    if args.csv:
        write_csv(args.csv, full["manifest"])
    output = full if args.include_manifest else {key: value for key, value in full.items()
                                                 if key != "manifest"}
    print(json.dumps(output, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
