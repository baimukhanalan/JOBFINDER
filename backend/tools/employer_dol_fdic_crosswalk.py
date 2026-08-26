"""FDIC BankFind crosswalk for active DOL employers.

The free official BankFind API needs no API key.  A link requires an exact legal
name (representation-only normalization), an independently shared state, and an
exact city or five-digit postal code.  FDIC-reported websites are candidate
structured factors only; this module never marks a domain verified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from psycopg2.extras import Json, RealDictCursor

from backend.tools import company_discovery_db as company_db
from backend.tools.employer_authoritative_sources import (
    FDIC_INSTITUTIONS_URL, fetch_fdic_institutions,
)
from backend.tools.employer_official_crosswalk import address_key, exact_legal_key, state_keys


PROVIDER = "fdic_bankfind"
ADDRESS_TYPE = "fdic_institution_headquarters_location"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _postal(value: Any) -> str:
    clean = re.sub(r"[^0-9]", "", str(value or ""))
    return clean[:5] if len(clean) >= 5 else clean


def _dol_location(row: Mapping[str, Any]) -> dict[str, Any]:
    address = _mapping(_mapping(row.get("metadata")).get("employer_address"))
    return {
        "states": state_keys(row.get("states"), address.get("region"), address.get("state")),
        "city": address_key(address.get("city")),
        "postal": _postal(address.get("postal_code") or address.get("zip")),
    }


def _fdic_location(node: Mapping[str, Any]) -> dict[str, Any]:
    attrs = _mapping(node.get("attributes"))
    return {"states": state_keys(attrs.get("state")),
            "city": address_key(attrs.get("city")), "postal": _postal(attrs.get("zip"))}


def _location_match(target: Mapping[str, Any], node: Mapping[str, Any]) -> dict | None:
    left, right = _dol_location(target), _fdic_location(node)
    shared_states = sorted(left["states"] & right["states"])
    if not shared_states:
        return None
    methods = []
    if left["city"] and right["city"] and left["city"] == right["city"]:
        methods.append("state_city")
    if left["postal"] and right["postal"] and left["postal"] == right["postal"]:
        methods.append("state_postal")
    if not methods:
        return None
    return {"exact_legal_name_key": exact_legal_key(target.get("legal_name")),
            "shared_states": shared_states, "methods": methods,
            "dol_disclosure_address_role": "identity_corroboration_only"}


def _domain_factor(node: Mapping[str, Any]) -> dict | None:
    assertions = [dict(item) for item in node.get("domain_assertions") or []
                  if isinstance(item, Mapping)
                  and item.get("entity_id") == node.get("entity_id")]
    domains = {str(item.get("domain") or "") for item in assertions if item.get("domain")}
    if len(domains) != 1:
        return None
    assertion = assertions[0]
    provenance = _mapping(assertion.get("provenance"))
    return {"class": "structured_corporate_source", "provider": PROVIDER,
            "entity_id": node["entity_id"], "candidate_domain": next(iter(domains)),
            "assertion": assertion.get("assertion_type"),
            "source_field": assertion.get("source_field"),
            "source_url": provenance.get("source_url"),
            "observed_at": provenance.get("observed_at"),
            "verification_status": "candidate_not_verified"}


def _stable_fingerprint_value(value: Any) -> Any:
    """Exclude retrieval time only; retain dataset version and every assertion input."""
    if isinstance(value, Mapping):
        return {key: _stable_fingerprint_value(item) for key, item in value.items()
                if key != "observed_at"}
    if isinstance(value, list):
        return [_stable_fingerprint_value(item) for item in value]
    return value


def build_plan_from_rows(targets: Iterable[Mapping[str, Any]],
                         nodes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    targets, nodes = list(targets), list(nodes)
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for node in nodes:
        if node.get("provider") == PROVIDER and node.get("entity_id"):
            by_name[exact_legal_key(node.get("legal_name"))].append(node)
    proposals: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for target in sorted(targets, key=lambda item: int(item["company_id"])):
        key = exact_legal_key(target.get("legal_name"))
        candidates = by_name.get(key, [])
        if not candidates:
            reasons["no_exact_legal_name"] += 1
            continue
        matched = [(node, evidence) for node in candidates
                   if (evidence := _location_match(target, node)) is not None]
        if len(matched) != 1:
            reasons["ambiguous_location" if len(matched) > 1 else "location_mismatch"] += 1
            continue
        node, match = matched[0]
        attrs = _mapping(node.get("attributes"))
        ids = _mapping(node.get("entity_ids"))
        current = _mapping(target.get("current"))
        existing_ids = _mapping(target.get("external_ids"))
        if (existing_ids.get("fdic_cert")
                and str(existing_ids["fdic_cert"]) != str(ids.get("fdic_cert"))):
            reasons["existing_fdic_id_conflict"] += 1
            continue
        factor = _domain_factor(node)
        candidate_domain = factor.get("candidate_domain") if factor else None
        existing_domain = str(current.get("candidate_domain") or "").lower()
        if candidate_domain and existing_domain and existing_domain != candidate_domain.lower():
            reasons["existing_candidate_domain_conflict"] += 1
            factor = None
            candidate_domain = None
        headquarters = ", ".join(part for part in (
            str(attrs.get("city") or "").strip(), str(attrs.get("state") or "").strip(),
            str(attrs.get("zip") or "").strip()) if part)
        fields: dict[str, dict[str, Any]] = {}
        source = _mapping(node.get("provenance"))
        base = {"provider": PROVIDER, "entity_id": node["entity_id"],
                "source_url": source.get("source_url") or FDIC_INSTITUTIONS_URL,
                "observed_at": source.get("observed_at"), "match": match}
        if not current.get("industry"):
            fields["industry"] = {"value": "FDIC-insured depository institution",
                                  "confidence": 0.98, "provenance": {
                                      **base, "source_field": "active institution dataset"}}
        if headquarters and not current.get("headquarters"):
            fields["headquarters"] = {"value": headquarters, "confidence": 0.98,
                                      "provenance": {**base,
                                          "source_field": "CITY/STALP/ZIP"}}
            fields["headquarters_address_type"] = {"value": ADDRESS_TYPE,
                                                    "confidence": 0.98,
                                                    "provenance": {**base,
                                                        "source_field": "CITY/STALP/ZIP"}}
        proposals.append({
            "company_id": int(target["company_id"]), "legal_name": target["legal_name"],
            "legal_name_key": key, "entity_id": node["entity_id"],
            "external_ids": {name: str(value) for name, value in ids.items() if value},
            "match": match, "candidate_domain": candidate_domain,
            "domain_factor": factor, "fields": fields,
            "source_attributes": {"bank_class": attrs.get("bank_class"),
                                  "active": attrs.get("active"),
                                  "dataset_timestamp": attrs.get("dataset_timestamp")},
            "source_provenance": source,
        })
    canonical = json.dumps(_stable_fingerprint_value(proposals), sort_keys=True,
                           separators=(",", ":"), default=str)
    return {"schema_version": 1, "provider": PROVIDER, "targets": len(targets),
            "source_records": len(nodes), "matched": len(proposals),
            "candidate_domains": sum(bool(item["candidate_domain"]) for item in proposals),
            "profile_field_updates": sum(len(item["fields"]) for item in proposals),
            "reasons": dict(sorted(reasons.items())), "proposals": proposals,
            "fingerprint": hashlib.sha256(canonical.encode()).hexdigest()}


def _load_targets(cur, *, for_update: bool = False) -> list[dict[str, Any]]:
    lock = " FOR UPDATE OF c,m" if for_update else ""
    cur.execute("""
      SELECT c.id company_id,c.legal_name,c.states,c.metadata,c.external_ids,
        jsonb_build_object('candidate_domain',m.candidate_domain,'industry',m.industry,
          'headquarters',m.headquarters,
          'headquarters_address_type',m.headquarters_address_type) current
      FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
      WHERE m.in_target_population AND c.source='dol_oflc_lca'
      ORDER BY c.id""" + lock)
    return [dict(row) for row in cur.fetchall()]


def dry_run(*, min_interval: float = 0.1) -> dict[str, Any]:
    nodes = fetch_fdic_institutions(limit=10_000, active_only=True, page_size=1000,
                                    max_pages=10, min_interval=min_interval)
    with company_db.conn() as connection:
        connection.set_session(readonly=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            targets = _load_targets(cur)
    return build_plan_from_rows(targets, nodes)


def apply_live(*, expected_fingerprint: str, min_interval: float = 0.1) -> dict[str, Any]:
    if not str(expected_fingerprint or "").strip():
        raise ValueError("--expected-fingerprint is required for apply")
    nodes = fetch_fdic_institutions(limit=10_000, active_only=True, page_size=1000,
                                    max_pages=10, min_interval=min_interval)
    with company_db.conn() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            targets = _load_targets(cur, for_update=True)
            plan = build_plan_from_rows(targets, nodes)
            if plan["fingerprint"] != expected_fingerprint:
                raise RuntimeError("FDIC plan changed; run dry-run again")
            updated = 0
            for item in plan["proposals"]:
                fields = {key: value["value"] for key, value in item["fields"].items()}
                field_evidence = {key: {"confidence": value["confidence"],
                                        **value["provenance"]}
                                  for key, value in item["fields"].items()}
                factor = item.get("domain_factor")
                cur.execute("""
                  UPDATE company_discovery c SET
                    external_ids=c.external_ids || %s,
                    provenance=c.provenance || %s,updated_at=now()
                  FROM company_employer_master m
                  WHERE c.id=m.company_id AND m.in_target_population
                    AND c.source='dol_oflc_lca' AND c.id=%s
                """, (Json(item["external_ids"]), Json({"fdic_crosswalk": {
                    "entity_id": item["entity_id"], "match": item["match"],
                    "source": item["source_provenance"]}}), item["company_id"]))
                if cur.rowcount != 1:
                    raise RuntimeError("active DOL target changed during FDIC apply")
                cur.execute("""
                  UPDATE company_employer_master SET
                    candidate_domain=COALESCE(candidate_domain,%s),
                    domain_evidence=COALESCE((SELECT jsonb_agg(e)
                      FROM jsonb_array_elements(domain_evidence) e
                      WHERE NOT (e->>'provider'=%s AND e->>'entity_id'=%s)),
                      '[]'::jsonb) || %s,
                    industry=COALESCE(industry,%s),headquarters=COALESCE(headquarters,%s),
                    headquarters_address_type=COALESCE(headquarters_address_type,%s),
                    qualification_evidence=qualification_evidence || %s,
                    identity_enrichment_provenance=identity_enrichment_provenance || %s,
                    identity_enriched_at=now(),updated_at=now()
                  WHERE in_target_population AND company_id=%s
                """, (item.get("candidate_domain"), PROVIDER, item["entity_id"],
                      Json([factor] if factor else []),
                      fields.get("industry"), fields.get("headquarters"),
                      fields.get("headquarters_address_type"), Json({"fdic_crosswalk": {
                          "entity_id": item["entity_id"], "match": item["match"],
                          "domain_status": "candidate_not_verified" if factor else "not_asserted",
                          "source_attributes": item["source_attributes"]}}),
                      Json({"fdic_crosswalk": {"fingerprint": plan["fingerprint"],
                                               "fields": field_evidence}}),
                      item["company_id"]))
                if cur.rowcount != 1:
                    raise RuntimeError("active DOL target changed during FDIC profile apply")
                updated += 1
    return {**{key: value for key, value in plan.items() if key != "proposals"},
            "applied": True, "updated": updated}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--min-interval", type=float, default=0.1)
    parser.add_argument("--include-proposals", action="store_true")
    args = parser.parse_args(argv)
    if args.min_interval < 0.05:
        raise ValueError("--min-interval must be at least 0.05 seconds")
    result = (apply_live(expected_fingerprint=args.expected_fingerprint or "",
                         min_interval=args.min_interval) if args.apply
              else dry_run(min_interval=args.min_interval))
    if not args.include_proposals:
        result = {key: value for key, value in result.items() if key != "proposals"}
    result.setdefault("applied", False)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
