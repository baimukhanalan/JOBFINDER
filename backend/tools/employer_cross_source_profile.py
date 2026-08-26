"""Conservative stored-source profile enrichment for active DOL employers.

Dry-run by default.  Matching requires an exact normalized legal name and an
independently shared state.  City/postal must also agree when both sources publish
them.  The DOL disclosed employer address is match evidence only, never HQ evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Mapping

from psycopg2.extras import Json, RealDictCursor

from backend.tools import company_discovery_db as company_db
from backend.tools.employer_master import _exact_legal_name_key


SUPPORTED_SOURCES = ("everify_large_employer", "wikidata_employer")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _location(row: Mapping[str, Any], *, dol: bool = False) -> dict:
    metadata = dict(row.get("metadata") or {})
    if dol:
        address = dict(metadata.get("employer_address") or {})
        return {
            "states": {value.upper() for value in row.get("states") or [] if _text(value)},
            "city": _text(address.get("city")).casefold(),
            "postal": re.sub(r"[^a-z0-9]", "", _text(address.get("postal_code")).casefold()),
        }
    states = {value.upper() for value in row.get("states") or [] if _text(value)}
    address: Mapping[str, Any] = {}
    for key in ("operational_headquarters", "headquarters_address"):
        if isinstance(metadata.get(key), Mapping):
            address = metadata[key]
            break
    snapshot = metadata.get("source_snapshot")
    if not address and isinstance(snapshot, Mapping):
        for key in ("operational_headquarters", "headquarters_address"):
            if isinstance(snapshot.get(key), Mapping):
                address = snapshot[key]
                break
    region = _text(address.get("region") or address.get("state"))
    if region:
        states.add(region.upper())
    city = _text(address.get("city"))
    if not city and row.get("source") == "wikidata_employer":
        city = _text(metadata.get("headquarters"))
    return {
        "states": states,
        "city": city.casefold(),
        "postal": re.sub(r"[^a-z0-9]", "", _text(
            address.get("postal_code") or address.get("postalCode")).casefold()),
    }


def _compatible(target: Mapping[str, Any], source: Mapping[str, Any]) -> tuple[bool, dict]:
    target_location = _location(target, dol=True)
    source_location = _location(source)
    shared_states = sorted(target_location["states"] & source_location["states"])
    if not shared_states:
        return False, {"reason": "state_not_independently_agreed"}
    for field in ("city", "postal"):
        left, right = target_location[field], source_location[field]
        if left and right and left != right:
            return False, {"reason": f"{field}_conflict", "dol": left, "source": right}
    return True, {
        "exact_legal_name_key": _exact_legal_name_key(target.get("legal_name")),
        "shared_states": shared_states,
        "city_agreed": bool(target_location["city"] and source_location["city"]),
        "postal_agreed": bool(target_location["postal"] and source_location["postal"]),
    }


def _field(value: Any, *, provider: str, source_row: Mapping[str, Any],
           source_field: str, confidence: float, match: dict,
           source_external_id: str | None = None,
           source_url: str | None = None) -> dict | None:
    if value in (None, "", [], {}):
        return None
    return {"value": value, "confidence": confidence, "provenance": {
        "provider": provider, "source_company_id": int(source_row["company_id"]),
        "source_external_id": source_external_id or _text(source_row.get("source_external_id")),
        "source_url": source_url or source_row.get("source_url"), "source_field": source_field,
        "match": match,
    }}


def _source_fields(row: Mapping[str, Any], match: dict) -> dict[str, dict]:
    provider = _text(row.get("source"))
    metadata = dict(row.get("metadata") or {})
    fields: dict[str, dict | None] = {}
    brand = _text(metadata.get("brand_name") or row.get("trade_name"))
    legal = _text(row.get("legal_name"))
    if brand and _exact_legal_name_key(brand) != _exact_legal_name_key(legal):
        fields["brand_name"] = _field(
            brand, provider=provider, source_row=row, source_field="metadata.brand_name",
            confidence=0.86 if provider == "everify_large_employer" else 0.76,
            match=match)
    if provider == "everify_large_employer":
        fields["employee_count_min"] = _field(
            metadata.get("employee_count_min"), provider=provider, source_row=row,
            source_field="metadata.employee_count_min", confidence=0.90, match=match)
        fields["employee_count_max"] = _field(
            metadata.get("employee_count_max"), provider=provider, source_row=row,
            source_field="metadata.employee_count_max", confidence=0.90, match=match)
        if fields.get("employee_count_min") or fields.get("employee_count_max"):
            fields["employee_size_source"] = _field(
                "E-Verify disclosed workforce range", provider=provider, source_row=row,
                source_field="metadata.workforce_range", confidence=0.90, match=match)
        # Some stored E-Verify profiles were independently matched to Wikidata.
        # A populated master column alone is not evidence: transfer it only when
        # the stored qualification and field-level provenance identify the exact
        # Wikidata property that asserted it.
        stored = dict(row.get("stored_profile") or {})
        qualification = dict(stored.get("qualification_evidence") or {})
        provenance = dict(stored.get("identity_enrichment_provenance") or {})
        field_sources = dict(provenance.get("field_sources") or {})
        qid = _text(qualification.get("wikidata_entity"))
        structured_match = qualification.get("structured_name_match") is True and bool(qid)
        wikidata_url = f"https://www.wikidata.org/wiki/{qid}" if qid else None
        if structured_match and stored.get("employee_size_source") == "wikidata:P1128":
            fields["employee_count"] = _field(
                stored.get("employee_count"), provider="wikidata", source_row=row,
                source_field="P1128", confidence=0.72, match=match,
                source_external_id=qid, source_url=wikidata_url)
        if structured_match and stored.get("industry") and (
                field_sources.get("industry") ==
                "company_discovery.industry_or_stored_structured_evidence"):
            fields["industry"] = _field(
                stored.get("industry"), provider="wikidata", source_row=row,
                source_field="P452", confidence=0.70, match=match,
                source_external_id=qid, source_url=wikidata_url)
        if (structured_match and stored.get("headquarters")
                and stored.get("headquarters_address_type") == "operational"
                and field_sources.get("headquarters") ==
                "qualification_evidence.wikidata_entity/P159"):
            fields["headquarters"] = _field(
                stored.get("headquarters"), provider="wikidata", source_row=row,
                source_field="P159", confidence=0.74, match=match,
                source_external_id=qid, source_url=wikidata_url)
            fields["headquarters_address_type"] = _field(
                "wikidata_P159_headquarters_location", provider="wikidata", source_row=row,
                source_field="P159", confidence=0.74, match=match,
                source_external_id=qid, source_url=wikidata_url)
    elif provider == "wikidata_employer":
        fields["employee_count"] = _field(
            metadata.get("employee_count"), provider=provider, source_row=row,
            source_field="P1128", confidence=0.72, match=match)
        fields["industry"] = _field(
            row.get("industry"), provider=provider, source_row=row,
            source_field="P452", confidence=0.70, match=match)
        headquarters = _text(metadata.get("headquarters"))
        if headquarters:
            fields["headquarters"] = _field(
                headquarters, provider=provider, source_row=row,
                source_field="P159", confidence=0.74, match=match)
            fields["headquarters_address_type"] = _field(
                "wikidata_P159_headquarters_location", provider=provider, source_row=row,
                source_field="P159", confidence=0.74, match=match)
    return {key: value for key, value in fields.items() if value is not None}


def build_plan_from_rows(targets: list[dict], sources: list[dict], *, limit: int = 10_000) -> dict:
    source_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in sources:
        source_groups[(_text(row.get("source")),
                       _exact_legal_name_key(row.get("legal_name")))].append(row)
    matches = []
    counters = defaultdict(int)
    for target in sorted(targets, key=lambda row: int(row["company_id"]))[:max(1, int(limit))]:
        key = _exact_legal_name_key(target.get("legal_name"))
        proposed: dict[str, dict] = {}
        providers = []
        for provider in SUPPORTED_SOURCES:
            candidates = source_groups.get((provider, key), [])
            if len(candidates) > 1:
                counters["ambiguous_source_keys"] += 1
                continue
            if not candidates:
                continue
            source = candidates[0]
            compatible, match = _compatible(target, source)
            if not compatible:
                counters[match["reason"]] += 1
                continue
            fields = _source_fields(source, match)
            if not fields:
                counters["matched_without_transferable_fields"] += 1
                continue
            providers.append(provider)
            for field_name, assertion in fields.items():
                existing = proposed.get(field_name)
                if existing and existing["value"] != assertion["value"]:
                    proposed.pop(field_name, None)
                    counters["field_conflicts"] += 1
                    continue
                proposed[field_name] = assertion
        current = dict(target.get("current") or {})
        # Do not attach a source/type label to a different pre-existing value.
        # Paired evidence may still fill a missing label when the stored value is
        # exactly the same as the independently asserted one.
        proposed_hq = proposed.get("headquarters")
        current_hq = _text(current.get("headquarters"))
        if (current_hq and proposed_hq
                and current_hq.casefold() != _text(proposed_hq["value"]).casefold()):
            proposed.pop("headquarters_address_type", None)
            counters["existing_headquarters_conflict"] += 1
        size_assertions_agree = any(
            proposed.get(field) and (
                current.get(field) in (None, "")
                or current.get(field) == proposed[field]["value"])
            for field in ("employee_count", "employee_count_min", "employee_count_max")
        )
        if proposed.get("employee_size_source") and not size_assertions_agree:
            proposed.pop("employee_size_source", None)
            counters["existing_employee_size_conflict"] += 1
        current_brand = _text(current.get("brand_name"))
        proposed_brand = proposed.get("brand_name")
        if current_brand and proposed_brand:
            if (_exact_legal_name_key(current_brand) == key
                    and _exact_legal_name_key(proposed_brand["value"]) != key):
                proposed_brand["replace_legal_name_fallback"] = True
            else:
                proposed.pop("brand_name", None)
        proposed = {name: assertion for name, assertion in proposed.items()
                    if (current.get(name) in (None, "", {}, [])
                        or assertion.get("replace_legal_name_fallback") is True)}
        if not proposed:
            continue
        matches.append({
            "company_id": int(target["company_id"]), "legal_name": target["legal_name"],
            "exact_legal_name_key": key, "providers": sorted(providers),
            "fields": proposed,
        })
    canonical = json.dumps(matches, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    return {"schema_version": 1, "targets": len(targets), "source_rows": len(sources),
            "matches": matches, "matched": len(matches),
            "field_updates": sum(len(row["fields"]) for row in matches),
            "diagnostics": dict(sorted(counters.items())), "fingerprint": fingerprint}


def _load_rows(cur, *, for_update: bool = False) -> tuple[list[dict], list[dict]]:
    lock = " FOR UPDATE OF c,m" if for_update else ""
    cur.execute("""
      SELECT c.id company_id,c.source,c.source_external_id,c.source_url,c.legal_name,
        c.trade_name,c.states,c.industry,c.metadata,
        jsonb_build_object('brand_name',m.brand_name,'employee_count',m.employee_count,
          'employee_count_min',m.employee_count_min,'employee_count_max',m.employee_count_max,
          'employee_size_source',m.employee_size_source,'industry',m.industry,
          'headquarters',m.headquarters,
          'headquarters_address_type',m.headquarters_address_type) current
      FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
      WHERE m.in_target_population AND c.source='dol_oflc_lca'
      ORDER BY c.id""" + lock)
    targets = [dict(row) for row in cur.fetchall()]
    cur.execute("""
      SELECT c.id company_id,c.source,c.source_external_id,c.source_url,c.legal_name,
        c.trade_name,c.states,c.industry,c.metadata,
        jsonb_build_object('brand_name',m.brand_name,'employee_count',m.employee_count,
          'employee_count_min',m.employee_count_min,'employee_count_max',m.employee_count_max,
          'employee_size_source',m.employee_size_source,'industry',m.industry,
          'headquarters',m.headquarters,
          'headquarters_address_type',m.headquarters_address_type,
          'qualification_evidence',m.qualification_evidence,
          'identity_enrichment_provenance',m.identity_enrichment_provenance) stored_profile
      FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
      WHERE c.source=ANY(%s)
      ORDER BY c.source,c.id""" + lock, (list(SUPPORTED_SOURCES),))
    return targets, [dict(row) for row in cur.fetchall()]


def build_plan(*, limit: int = 10_000) -> dict:
    with company_db.conn() as connection:
        connection.set_session(readonly=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            targets, sources = _load_rows(cur)
    return build_plan_from_rows(targets, sources, limit=limit)


def apply_plan(*, expected_fingerprint: str, limit: int = 10_000) -> dict:
    if not _text(expected_fingerprint):
        raise ValueError("--expected-fingerprint is required for apply")
    with company_db.conn() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            targets, sources = _load_rows(cur, for_update=True)
            plan = build_plan_from_rows(targets, sources, limit=limit)
            if plan["fingerprint"] != expected_fingerprint:
                raise RuntimeError("cross-source profile plan changed; run dry-run again")
            updated = 0
            for match in plan["matches"]:
                values = {name: assertion["value"] for name, assertion in match["fields"].items()}
                evidence = {name: {"confidence": assertion["confidence"],
                                   **assertion["provenance"]}
                            for name, assertion in match["fields"].items()}
                brand = values.get("brand_name")
                replace_brand_fallback = bool(
                    match["fields"].get("brand_name", {}).get(
                        "replace_legal_name_fallback"))
                brand_identity = {"brand_name": brand, "source": "cross_source_exact_match"} \
                    if brand else {}
                cur.execute("""
                  UPDATE company_employer_master SET
                    brand_name=CASE WHEN %s THEN %s ELSE COALESCE(brand_name,%s) END,
                    employee_count=COALESCE(employee_count,%s),
                    employee_count_min=COALESCE(employee_count_min,%s),
                    employee_count_max=COALESCE(employee_count_max,%s),
                    employee_size_source=COALESCE(employee_size_source,%s),
                    industry=COALESCE(industry,%s),headquarters=COALESCE(headquarters,%s),
                    headquarters_address_type=COALESCE(headquarters_address_type,%s),
                    brand_identity=brand_identity || %s,
                    identity_enrichment_provenance=identity_enrichment_provenance || %s,
                    identity_enriched_at=now(),updated_at=now()
                  WHERE company_id=%s AND in_target_population
                """, (replace_brand_fallback, brand, brand,
                      values.get("employee_count"), values.get("employee_count_min"),
                      values.get("employee_count_max"), values.get("employee_size_source"),
                      values.get("industry"), values.get("headquarters"),
                      values.get("headquarters_address_type"), Json(brand_identity),
                      Json({"cross_source_profile": {"fingerprint": plan["fingerprint"],
                                                     "fields": evidence}}),
                      match["company_id"]))
                if cur.rowcount != 1:
                    raise RuntimeError("active DOL target changed during profile apply")
                updated += 1
    return {**{key: value for key, value in plan.items() if key != "matches"},
            "applied": True, "updated": updated}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--include-matches", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        raise ValueError("--limit must be positive")
    result = apply_plan(expected_fingerprint=args.expected_fingerprint or "",
                        limit=args.limit) if args.apply else build_plan(limit=args.limit)
    if not args.include_matches:
        result = {key: value for key, value in result.items() if key != "matches"}
    result.setdefault("applied", False)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
