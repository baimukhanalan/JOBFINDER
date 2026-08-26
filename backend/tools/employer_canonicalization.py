"""Conservative canonicalization proposals for the active employer population.

Name, brand, domain and parent/subsidiary similarity are review evidence only.
Automatic canonical links require a shared durable official entity ID, or an exact
legal-name plus exact structured official-address match where both records carry
official IDs and no ID namespace conflicts.  Dry-run is the CLI default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.tools.employer_population_quality import (
    organization_name_key, typography_name_key,
)


CONTRACT_VERSION = 1
_OFFICIAL_ID_PATTERNS = {
    "lei": re.compile(r"[A-Z0-9]{20}"),
    "sam_uei": re.compile(r"[A-Z0-9]{12}"),
    "fdic_cert": re.compile(r"\d+"),
    "sec_cik": re.compile(r"\d{10}"),
}
_SOURCE_PRIORITY = {
    "mandatory_employer": 5, "everify_large_employer": 4,
    "wikidata_employer": 3, "usaspending": 2, "gleif_lei": 1,
}
_STREET_WORDS = {
    "street": "st", "st": "st", "road": "rd", "rd": "rd",
    "avenue": "ave", "ave": "ave", "boulevard": "blvd", "blvd": "blvd",
    "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln",
    "highway": "hwy", "hwy": "hwy", "parkway": "pkwy", "pkwy": "pkwy",
    "suite": "ste", "ste": "ste", "floor": "fl", "building": "bldg",
}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tokens(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    return re.findall(r"[a-z0-9]+", text.replace("&", " and "))


def _normalized_words(value: Any, *, street: bool = False) -> str:
    words = _tokens(value)
    if street:
        words = [_STREET_WORDS.get(word, word) for word in words]
    return " ".join(words)


def _official_ids(record: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    raw_ids = _mapping(record.get("external_ids"))
    for kind, pattern in _OFFICIAL_ID_PATTERNS.items():
        value = _text(raw_ids.get(kind)).upper()
        if pattern.fullmatch(value):
            output[kind] = value
    source = _text(record.get("source"))
    external = _text(record.get("source_external_id")).upper()
    if source == "gleif_lei" and _OFFICIAL_ID_PATTERNS["lei"].fullmatch(external):
        output["lei"] = external
    evidence = _mapping(record.get("qualification_evidence"))
    gleif = _mapping(evidence.get("gleif_entity"))
    lei = _text(gleif.get("lei")).upper()
    if _OFFICIAL_ID_PATTERNS["lei"].fullmatch(lei):
        output["lei"] = lei
    fdic = _mapping(evidence.get("fdic_official_enrichment"))
    entity_id = _text(fdic.get("entity_id"))
    if entity_id.startswith("fdic_cert:"):
        cert = entity_id.split(":", 1)[1]
        if _OFFICIAL_ID_PATTERNS["fdic_cert"].fullmatch(cert):
            output["fdic_cert"] = cert
    return dict(sorted(output.items()))


def _address_payloads(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    metadata = _mapping(record.get("metadata"))
    snapshot = _mapping(metadata.get("source_snapshot"))
    payloads: list[Mapping[str, Any]] = []
    for key in ("legal_address", "headquarters_address"):
        if value := _mapping(snapshot.get(key)):
            payloads.append(value)
    for item in snapshot.get("addresses") or []:
        if value := _mapping(_mapping(item).get("value")):
            payloads.append(value)
    if value := _mapping(_mapping(snapshot.get("recipient_location")).get("value")):
        payloads.append(value)
    fdic = _mapping(_mapping(record.get("qualification_evidence")).get(
        "fdic_official_enrichment"))
    if value := _mapping(_mapping(fdic.get("main_office")).get("value")):
        payloads.append(value)
    return payloads


def _address_key(value: Mapping[str, Any]) -> str:
    lines = value.get("addressLines")
    if not isinstance(lines, list):
        lines = [value.get(key) for key in (
            "address_line1", "addressLine1", "ADDRESS", "street", "line1")]
    clean_lines = [_text(line) for line in lines if _text(line)]
    street_line = next((line for line in clean_lines
                        if re.search(r"\d", line) and not re.match(r"c/?o\b", line, re.I)), "")
    if not street_line and clean_lines:
        street_line = clean_lines[0]
    street = _normalized_words(street_line, street=True)
    city = _normalized_words(
        value.get("city") or value.get("CITY") or value.get("city_name"))
    state = _normalized_words(
        value.get("region") or value.get("STALP") or value.get("state_code")
        or value.get("state"))
    if state.startswith("us "):
        state = state[3:]
    postal = re.sub(r"\D", "", _text(
        value.get("postalCode") or value.get("ZIP") or value.get("zip")))[:5]
    country = _normalized_words(
        value.get("country") or value.get("country_code") or "us")
    if country in {"usa", "united states", "united states of america"}:
        country = "us"
    if not street or not (postal or (city and state)):
        return ""
    return "|".join((street, city, state, postal, country))


def _addresses(record: Mapping[str, Any]) -> set[str]:
    return {key for payload in _address_payloads(record)
            if (key := _address_key(payload))}


def _domain(record: Mapping[str, Any]) -> str:
    raw = _text(record.get("domain")).casefold()
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or "").strip(".").removeprefix("www.")


def _record(record: Mapping[str, Any]) -> dict[str, Any]:
    legal_name = _text(record.get("legal_name"))
    brand = _text(record.get("brand_name") or record.get("trade_name") or legal_name)
    return {
        **dict(record),
        "company_id": int(record.get("company_id") or record.get("id")),
        "legal_name": legal_name,
        "brand_name": brand,
        "exact_name_key": typography_name_key(legal_name),
        "variant_name_key": organization_name_key(legal_name),
        "brand_key": organization_name_key(brand),
        "official_ids_normalized": _official_ids(record),
        "official_address_keys": sorted(_addresses(record)),
        "normalized_domain": _domain(record),
    }


def _groups(rows: Sequence[Mapping[str, Any]], field: str) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _text(row.get(field))
        if key:
            grouped[key].append(dict(row))
    return [sorted(group, key=lambda row: row["company_id"])
            for _, group in sorted(grouped.items()) if len(group) > 1]


def _conflicting_id_types(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for kind, value in _mapping(row.get("official_ids_normalized")).items():
            values[kind].add(str(value))
    return {kind: sorted(items) for kind, items in values.items() if len(items) > 1}


def _canonical(rows: Sequence[Mapping[str, Any]]) -> int:
    def key(row: Mapping[str, Any]) -> tuple:
        metadata = _mapping(row.get("metadata"))
        proven = metadata.get("employer_evidence_level") == "proven"
        return (-int(bool(row.get("mandatory_seed"))), -int(proven),
                -_SOURCE_PRIORITY.get(_text(row.get("source")), 0),
                int(row["company_id"]))
    return int(min(rows, key=key)["company_id"])


class _UnionFind:
    def __init__(self, values: Sequence[int]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def build_canonicalization_report(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build deterministic apply-safe and review-only canonicalization evidence."""
    rows = sorted((_record(row) for row in records), key=lambda row: row["company_id"])
    by_id = {row["company_id"]: row for row in rows}
    union = _UnionFind(list(by_id))
    apply_edges: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    official_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for kind, value in row["official_ids_normalized"].items():
            official_groups[(kind, value)].append(row)
    for (kind, value), group in sorted(official_groups.items()):
        if len(group) < 2:
            continue
        conflicts = _conflicting_id_types(group)
        evidence = {"rule": "same_durable_entity_id", "official_id_type": kind,
                    "official_id": value,
                    "member_company_ids": [row["company_id"] for row in group]}
        if conflicts:
            review.append({**evidence, "decision": "review",
                           "reason": "conflicting_official_ids",
                           "conflicting_ids": conflicts})
            continue
        for row in group[1:]:
            union.union(group[0]["company_id"], row["company_id"])
        apply_edges.append(evidence)

    legal_address_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row["official_ids_normalized"] or not row["exact_name_key"]:
            continue
        for address in row["official_address_keys"]:
            legal_address_groups[(row["exact_name_key"], address)].append(row)
    for (name, address), raw_group in sorted(legal_address_groups.items()):
        group = list({row["company_id"]: row for row in raw_group}.values())
        if len(group) < 2:
            continue
        conflicts = _conflicting_id_types(group)
        evidence = {"rule": "exact_legal_address_with_official_ids",
                    "exact_name_key": name, "official_address_key": address,
                    "member_company_ids": sorted(row["company_id"] for row in group),
                    "official_ids": {str(row["company_id"]): row["official_ids_normalized"]
                                     for row in group}}
        if conflicts:
            review.append({**evidence, "decision": "review",
                           "reason": "distinct_durable_entities_same_name_address",
                           "conflicting_ids": conflicts})
            continue
        for row in group[1:]:
            union.union(group[0]["company_id"], row["company_id"])
        apply_edges.append(evidence)

    components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        components[union.find(row["company_id"])].append(row)
    proposals: list[dict[str, Any]] = []
    for group in sorted((items for items in components.values() if len(items) > 1),
                        key=lambda items: min(row["company_id"] for row in items)):
        member_ids = sorted(row["company_id"] for row in group)
        existing = {int(row["canonical_company_id"]) for row in group
                    if row.get("canonical_company_id") is not None}
        outside = sorted(existing - set(member_ids))
        evidence = [edge for edge in apply_edges
                    if len(set(edge["member_company_ids"]) & set(member_ids)) > 1]
        if outside:
            review.append({"decision": "review", "rule": "existing_canonical_conflict",
                           "member_company_ids": member_ids,
                           "existing_canonical_company_ids": outside,
                           "evidence": evidence})
            continue
        proposals.append({
            "decision": "apply_safe", "canonical_company_id": _canonical(group),
            "member_company_ids": member_ids, "evidence": evidence,
            "provenance": {"contract": "durable_entity_canonicalization",
                           "contract_version": CONTRACT_VERSION},
        })

    exact_clusters = [{"key": group[0]["exact_name_key"],
                       "member_company_ids": [row["company_id"] for row in group],
                       "names": sorted({_text(row["legal_name"]) for row in group}),
                       "decision": "review_name_only"}
                      for group in _groups(rows, "exact_name_key")]
    variant_clusters = [{"key": group[0]["variant_name_key"],
                         "member_company_ids": [row["company_id"] for row in group],
                         "names": sorted({_text(row["legal_name"]) for row in group}),
                         "decision": "review_name_only"}
                        for group in _groups(rows, "variant_name_key")]

    families: list[dict[str, Any]] = []
    for field, rule in (("brand_key", "shared_brand_variant"),
                        ("normalized_domain", "shared_official_domain")):
        for group in _groups(rows, field):
            families.append({"decision": "review_distinct_entities", "rule": rule,
                             "key": group[0][field],
                             "member_company_ids": [row["company_id"] for row in group]})
    official_id_to_company = {
        (kind, value): row["company_id"] for row in rows
        for kind, value in row["official_ids_normalized"].items()
    }
    for row in rows:
        snapshot = _mapping(_mapping(row.get("metadata")).get("source_snapshot"))
        parent_uei = _text(snapshot.get("parent_uei")).upper()
        own_uei = _text(row["official_ids_normalized"].get("sam_uei")).upper()
        if not parent_uei or parent_uei == own_uei:
            continue
        parent_id = official_id_to_company.get(("sam_uei", parent_uei))
        families.append({
            "decision": "review_distinct_entities", "rule": "official_parent_uei",
            "parent_company_id": parent_id,
            "child_company_id": row["company_id"], "parent_uei": parent_uei,
            "parent_name": _text(snapshot.get("parent_name")),
        })
    families.sort(key=lambda item: (item["rule"], json.dumps(item, sort_keys=True)))
    review.sort(key=lambda item: (item["rule"], json.dumps(item, sort_keys=True)))

    fingerprint_payload = [{key: row.get(key) for key in (
        "company_id", "legal_name", "brand_name", "source", "source_external_id",
        "official_ids_normalized", "official_address_keys", "normalized_domain",
        "canonical_company_id")} for row in rows]
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()
    return {
        "contract_version": CONTRACT_VERSION,
        "snapshot_fingerprint": fingerprint,
        "active_total": len(rows),
        "exact_duplicate_cluster_count": len(exact_clusters),
        "variant_duplicate_cluster_count": len(variant_clusters),
        "parent_subsidiary_family_count": len(families),
        "apply_safe_proposal_count": len(proposals),
        "apply_safe_member_count": sum(len(item["member_company_ids"]) - 1
                                       for item in proposals),
        "review_conflict_count": len(review),
        "exact_duplicate_clusters": exact_clusters,
        "variant_duplicate_clusters": variant_clusters,
        "parent_subsidiary_families": families,
        "apply_safe_proposals": proposals,
        "review_conflicts": review,
    }


def load_active_records() -> list[dict[str, Any]]:
    from backend.tools import company_discovery_db as company_db
    with company_db.conn() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("""
              SELECT c.id AS company_id,c.source,c.source_external_id,c.external_ids,
                c.legal_name,c.trade_name,c.domain,c.metadata,c.provenance,
                m.brand_name,m.mandatory_seed,m.canonical_company_id,
                m.qualification_evidence,m.headquarters,m.headquarters_country,
                m.headquarters_address_type
              FROM company_employer_master m
              JOIN company_discovery c ON c.id=m.company_id
              WHERE m.in_target_population ORDER BY c.id
            """)
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def apply_canonicalization(report: Mapping[str, Any], *, expected_fingerprint: str) -> int:
    """Apply only apply-safe links from a caller-confirmed, current snapshot."""
    if not expected_fingerprint or expected_fingerprint != report.get("snapshot_fingerprint"):
        raise ValueError("expected fingerprint does not match canonicalization report")
    proposals = list(report.get("apply_safe_proposals") or [])
    if not proposals:
        return 0
    from backend.tools import company_discovery_db as company_db
    updated = 0
    with company_db.conn() as connection:
        with connection.cursor() as cursor:
            for proposal in proposals:
                canonical = int(proposal["canonical_company_id"])
                members = [int(value) for value in proposal["member_company_ids"]]
                evidence = json.dumps({
                    "decision": "apply_safe", "canonical_company_id": canonical,
                    "evidence": proposal["evidence"],
                    "snapshot_fingerprint": expected_fingerprint,
                    "contract_version": CONTRACT_VERSION,
                }, ensure_ascii=False, sort_keys=True)
                cursor.execute("""
                  UPDATE company_employer_master SET canonical_company_id=%s,
                    qualification_evidence=qualification_evidence ||
                      jsonb_build_object('canonicalization',%s::jsonb),updated_at=now()
                  WHERE in_target_population AND company_id=ANY(%s)
                    AND (canonical_company_id IS NULL
                         OR canonical_company_id=ANY(%s))
                """, (canonical, evidence, members, members))
                if cursor.rowcount != len(members):
                    raise RuntimeError("canonicalization changed or conflicted during apply")
                updated += cursor.rowcount
    return updated


def _summary(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: report[key] for key in (
        "contract_version", "snapshot_fingerprint", "active_total",
        "exact_duplicate_cluster_count", "variant_duplicate_cluster_count",
        "parent_subsidiary_family_count", "apply_safe_proposal_count",
        "apply_safe_member_count", "review_conflict_count")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe employer canonicalization proposal")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint", default="")
    args = parser.parse_args(argv)
    report = build_canonicalization_report(load_active_records())
    updated = apply_canonicalization(
        report, expected_fingerprint=args.expected_fingerprint) if args.apply else 0
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"applied": bool(args.apply), "updated": updated, **_summary(report)},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
