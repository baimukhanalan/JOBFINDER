"""Read-only quality lanes for the active employer population.

The classifier is deliberately conservative: it emits proposed ``keep``,
``review`` and ``quarantine`` decisions with reproducible evidence.  It never
updates employer state and must not be used as an automatic rejection writer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CONTRACT_VERSION = 1
LANE_PRIORITY = {"keep": 0, "review": 1, "quarantine": 2}
_LEGAL_SUFFIXES = frozenset({
    "co", "company", "corp", "corporation", "inc", "incorporated", "llc",
    "llp", "lp", "limited", "ltd", "plc", "pc", "pa",
})
_RULE_ORDER = {
    "missing_or_placeholder_name": 10,
    "duplicate_source_entity_id": 20,
    "aggregate_multi_entity_name": 30,
    "personal_or_family_trust": 40,
    "benefit_or_retirement_plan": 50,
    "special_purpose_financial_vehicle": 60,
    "fund_or_trust_entity": 70,
    "shell_or_payroll_entity": 80,
    "organizational_unit_name": 90,
    "holding_or_management_entity": 100,
    "abnormal_name_shape": 110,
    "source_artifact_in_name": 120,
    "exact_legal_duplicate": 130,
    "legal_variant_cluster": 140,
    "duplicate_brand_cluster": 150,
    "shared_domain_group": 160,
    "existing_canonical_link": 170,
    "legal_identity_only_source": 180,
    "activity_without_workforce_proof": 190,
}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def typography_name_key(value: Any) -> str:
    """Normalize typography while preserving legal suffix semantics."""
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text.replace("&", " and ")))


def organization_name_key(value: Any) -> str:
    """Normalize common legal suffix variants for duplicate candidate discovery."""
    words = typography_name_key(value).split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    if words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def _domain(value: Any) -> str:
    raw = _text(value).casefold()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or "").strip(".").removeprefix("www.")


def _primary_name(record: Mapping[str, Any]) -> str:
    return _text(record.get("legal_name") or record.get("brand_name")
                 or record.get("trade_name"))


def _brand_name(record: Mapping[str, Any]) -> str:
    return _text(record.get("brand_name") or record.get("trade_name")
                 or record.get("legal_name"))


def _evidence(rule: str, *, category: str, lane: str, field: str,
              match: Any = None, related_company_ids: Sequence[str] = ()) -> dict[str, Any]:
    output = {"rule": rule, "category": category, "proposed_lane": lane,
              "field": field}
    if match not in (None, "", []):
        output["match"] = match
    if related_company_ids:
        output["related_company_ids"] = sorted(set(related_company_ids))
    return output


def _regex_evidence(name: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    folded = name.casefold()
    rules = (
        ("aggregate_multi_entity_name", "aggregate_identity", "quarantine",
         r"(?:(?:\band\b|&)\s+(?:(?:all|its|integrated|operating|related)\s+){0,3}(?:subsidiar(?:y|ies)|affiliates?)\b|\baffiliated (?:or related )?entities\b|\bon behalf of\b|\bcollectively known as\b)"),
        ("personal_or_family_trust", "non_employer_legal_entity", "quarantine",
         r"\b(?:revocable|irrevocable|living|family|dynasty|testamentary) trusts?\b|\btrustees? of the .+ family trust\b|\bfbo\b.+\btrust\b|\btrust\b.+\bdated\b"),
        ("benefit_or_retirement_plan", "non_employer_legal_entity", "quarantine",
         r"\b(?:employee benefit|benefit plan|retirement plan|pension plan|profit sharing plan|401\s*\(?k\)?)\b"),
        ("special_purpose_financial_vehicle", "non_employer_legal_entity", "quarantine",
         r"\b(?:mortgage loan trust|asset[- ]backed|securiti[sz]ation|special purpose vehicle|investment vehicle|statutory trust|(?:investment|opportunity|credit|mortgage|real estate|hedge|private equity) fund)\b|\bfund\b.+\b(?:lp|l\.p\.)\b"),
        ("shell_or_payroll_entity", "shell_or_payroll", "review",
         r"\b(?:payroll|shared services|administrative services|employer of record|management services|management company)\b"),
        ("organizational_unit_name", "parent_or_subsidiary", "review",
         r"\b(?:subsidiar(?:y|ies)|affiliate(?:d|s)?|division of|division|branch|regional operations)\b"),
        ("holding_or_management_entity", "non_operating_entity", "review",
         r"\b(?:holding company|holdings|parent company)\b"),
    )
    for rule, category, lane, pattern in rules:
        if match := re.search(pattern, folded, re.I):
            findings.append(_evidence(
                rule, category=category, lane=lane, field="legal_name",
                match=match.group(0)))

    # A bare trust/fund is ambiguous and therefore review-only.  Trust companies,
    # banks, public-health trusts and university boards can be operating employers
    # and are not flagged by this fallback rule.
    if (re.search(r"\b(?:fund|trust)\b", folded)
            and not re.search(
                r"\b(?:trust (?:company|co\.?|bank)|bank and trust|public health trust|trustees? of .+ university|university trustees?)\b",
                folded)
            and not any(item["rule"] in {
                "personal_or_family_trust", "special_purpose_financial_vehicle",
                "benefit_or_retirement_plan",
            } for item in findings)):
        match = re.search(r"\b(?:fund|trust)\b", folded)
        findings.append(_evidence(
            "fund_or_trust_entity", category="non_employer_legal_entity",
            lane="review", field="legal_name", match=match.group(0)))
    return findings


def _record_evidence(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    name = _primary_name(record)
    folded = name.casefold()
    findings: list[dict[str, Any]] = []
    if (not typography_name_key(name)
            or folded in {"unknown", "n/a", "na", "none", "test", "company"}):
        findings.append(_evidence(
            "missing_or_placeholder_name", category="abnormal_name", lane="quarantine",
            field="legal_name", match=name or "<empty>"))
        return findings
    findings.extend(_regex_evidence(name))
    word_count = len(typography_name_key(name).split())
    digit_count = sum(char.isdigit() for char in name)
    if (len(name) > 80 or word_count > 12
            or (len(name) >= 8 and digit_count / len(name) >= 0.35)
            or re.search(r"https?://|@[^ ]+\.", name, re.I)
            or re.search(r"[_|]{1,}", name)):
        findings.append(_evidence(
            "abnormal_name_shape", category="abnormal_name", lane="review",
            field="legal_name",
            match={"length": len(name), "word_count": word_count,
                   "digit_ratio": round(digit_count / max(1, len(name)), 3)}))
    if re.search(r"\be-verify\+?$", folded):
        findings.append(_evidence(
            "source_artifact_in_name", category="abnormal_name", lane="review",
            field="legal_name", match="E-Verify suffix"))

    source = _text(record.get("source"))
    metadata = _mapping(_json_value(record.get("metadata")))
    evidence_level = _text(metadata.get("employer_evidence_level"))
    if source == "gleif_lei" or evidence_level == "candidate" and source == "gleif_lei":
        findings.append(_evidence(
            "legal_identity_only_source", category="non_employer_risk", lane="review",
            field="source", match=source))
    elif source == "usaspending" or evidence_level == "activity_backed":
        findings.append(_evidence(
            "activity_without_workforce_proof", category="non_employer_risk",
            lane="review", field="source", match=source))
    canonical = _text(record.get("canonical_company_id"))
    company_id = _text(record.get("company_id") or record.get("id"))
    if canonical and canonical != company_id:
        findings.append(_evidence(
            "existing_canonical_link", category="duplicate_or_parent", lane="review",
            field="canonical_company_id", match=canonical,
            related_company_ids=[canonical]))
    return findings


def _indexes(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, list[str]]]:
    indexes: dict[str, dict[str, list[str]]] = {
        key: defaultdict(list) for key in (
            "source_identity", "exact_legal", "legal_variant", "brand", "domain")
    }
    for record in records:
        company_id = _text(record.get("company_id") or record.get("id"))
        source = _text(record.get("source"))
        external_id = _text(record.get("source_external_id"))
        keys = {
            "source_identity": f"{source}:{external_id}" if source and external_id else "",
            "exact_legal": typography_name_key(_primary_name(record)),
            "legal_variant": organization_name_key(_primary_name(record)),
            "brand": organization_name_key(_brand_name(record)),
            "domain": _domain(record.get("domain")),
        }
        for index, key in keys.items():
            if key:
                indexes[index][key].append(company_id)
    return indexes


def _cluster_evidence(record: Mapping[str, Any], indexes: Mapping[str, Mapping[str, list[str]]]) -> list[dict[str, Any]]:
    company_id = _text(record.get("company_id") or record.get("id"))
    source = _text(record.get("source"))
    external_id = _text(record.get("source_external_id"))
    candidates = (
        ("source_identity", f"{source}:{external_id}" if source and external_id else "",
         "duplicate_source_entity_id", "duplicate_identity", "quarantine"),
        ("exact_legal", typography_name_key(_primary_name(record)),
         "exact_legal_duplicate", "duplicate_identity", "review"),
        ("legal_variant", organization_name_key(_primary_name(record)),
         "legal_variant_cluster", "duplicate_brand_legal_variant", "review"),
        ("brand", organization_name_key(_brand_name(record)),
         "duplicate_brand_cluster", "duplicate_brand_legal_variant", "review"),
        ("domain", _domain(record.get("domain")),
         "shared_domain_group", "parent_or_subsidiary", "review"),
    )
    findings: list[dict[str, Any]] = []
    for index, key, rule, category, lane in candidates:
        related = sorted(set(indexes[index].get(key, [])) - {company_id}) if key else []
        if related:
            findings.append(_evidence(
                rule, category=category, lane=lane, field=index, match=key,
                related_company_ids=related))
    return findings


def _decision(record: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in findings:
        item = dict(raw)
        unique[(item["rule"], json.dumps(item.get("match"), sort_keys=True))] = item
    evidence = sorted(unique.values(), key=lambda item: (
        _RULE_ORDER.get(item["rule"], 999), item["rule"],
        json.dumps(item, sort_keys=True)))
    lane = max((item["proposed_lane"] for item in evidence),
               key=lambda value: LANE_PRIORITY[value], default="keep")
    return {
        "company_id": _text(record.get("company_id") or record.get("id")),
        "legal_name": _primary_name(record),
        "brand_name": _brand_name(record),
        "source": _text(record.get("source")),
        "source_external_id": _text(record.get("source_external_id")),
        "proposed_lane": lane,
        "evidence": evidence,
        "provenance": {
            "contract": "active_employer_population_quality",
            "contract_version": CONTRACT_VERSION,
            "classification": "deterministic_read_only",
        },
    }


def classify_employer_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Classify intrinsic hard/review signals without population-level clusters."""
    return _decision(record, _record_evidence(record))


def classify_population(records: Sequence[Mapping[str, Any]], *, max_records: int = 10_000) -> dict[str, Any]:
    """Return deterministic proposed lanes for a bounded active-population snapshot."""
    if max_records < 1 or max_records > 10_000:
        raise ValueError("max_records must be between 1 and 10000")
    ordered = sorted((dict(row) for row in records),
                     key=lambda row: (_text(row.get("company_id") or row.get("id")),
                                      _primary_name(row)))[:max_records]
    indexes = _indexes(ordered)
    decisions: list[dict[str, Any]] = []
    for record in ordered:
        findings = _record_evidence(record) + _cluster_evidence(record, indexes)
        decisions.append(_decision(record, findings))

    signal_counts = Counter(
        evidence["rule"] for decision in decisions for evidence in decision["evidence"])
    category_counts = Counter(
        evidence["category"] for decision in decisions for evidence in decision["evidence"])
    lane_counts = Counter(decision["proposed_lane"] for decision in decisions)
    fingerprint_rows = [{key: decision[key] for key in (
        "company_id", "legal_name", "brand_name", "source", "source_external_id",
        "proposed_lane", "evidence")}
        for decision in decisions]
    snapshot_fingerprint = hashlib.sha256(json.dumps(
        fingerprint_rows, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    cluster_counts = {
        index: sum(len(set(company_ids)) > 1 for company_ids in values.values())
        for index, values in indexes.items()
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "input_total": len(records),
        "classified_total": len(decisions),
        "truncated": max(0, len(records) - len(decisions)),
        "snapshot_fingerprint": snapshot_fingerprint,
        "lane_counts": {lane: lane_counts.get(lane, 0)
                        for lane in ("keep", "review", "quarantine")},
        "signal_counts": dict(sorted(signal_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "duplicate_cluster_counts": cluster_counts,
        "decisions": decisions,
    }


def load_active_population() -> list[dict[str, Any]]:
    """Load the active population in a transaction explicitly marked read-only."""
    from backend.tools import company_discovery_db as company_db

    with company_db.conn() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("""
              SELECT m.company_id,m.brand_name,m.canonical_company_id,
                m.employer_segment,m.entity_risk_flags,m.brand_identity,m.industry,
                m.headquarters,m.headquarters_country,m.employee_count,
                m.employee_count_min,m.employee_count_max,m.mandatory_seed,
                m.identity_enrichment_status,m.identity_enrichment_provenance,
                m.identity_enrichment_gaps,c.legal_name,c.trade_name,c.source,
                c.source_external_id,c.states,c.naics,c.metadata,c.provenance,
                c.domain
              FROM company_employer_master m
              JOIN company_discovery c ON c.id=m.company_id
              WHERE m.in_target_population
              ORDER BY m.company_id
            """)
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def report_summary(report: Mapping[str, Any], *, examples_per_lane: int = 10) -> dict[str, Any]:
    examples: dict[str, list[dict[str, Any]]] = {}
    for lane in ("quarantine", "review", "keep"):
        examples[lane] = [decision for decision in report.get("decisions", [])
                          if decision.get("proposed_lane") == lane][:max(0, examples_per_lane)]
    return {key: report[key] for key in (
        "contract_version", "input_total", "classified_total", "truncated",
        "snapshot_fingerprint", "lane_counts", "signal_counts", "category_counts",
        "duplicate_cluster_counts")} | {"examples": examples}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only active employer quality audit")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--examples", type=int, default=10)
    args = parser.parse_args(argv)
    report = classify_population(load_active_population(), max_records=args.limit)
    payload = report if args.output else report_summary(report, examples_per_lane=args.examples)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report_summary(report, examples_per_lane=args.examples),
                     ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
