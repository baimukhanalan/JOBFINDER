"""CLI for the curated 10,000-candidate mass-hiring employer master."""
from __future__ import annotations

import argparse
import json
import sys

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_master_db as master_db
from backend.tools.employer_population_quality import classify_employer_record
from backend.tools.employer_segmentation import refresh_segments
from backend.tools.employer_sources import fetch_employer_reservoir


_SOURCE_PRIORITY = {
    "mandatory_employer": 4,
    "everify_large_employer": 3,
    "wikidata_employer": 3,
    "usaspending": 2,
    "gleif_lei": 1,
}
def _selection_score(row: dict) -> int:
    metadata = row.get("metadata") or {}
    source = str(row.get("source") or "")
    score = _SOURCE_PRIORITY.get(source, 0) * 1_000_000
    if source == "everify_large_employer":
        score += min(int(metadata.get("hiring_sites") or 0), 100_000)
    elif source == "wikidata_employer":
        score += min(int(metadata.get("employee_count") or 0) // 10, 100_000)
    elif source == "usaspending":
        score += min(int(float(metadata.get("amount") or 0)) // 1000, 50_000)
    elif source == "gleif_lei":
        score += 10_000 if metadata.get("entity_status") == "ACTIVE" else 0
    score -= 25_000 * len(metadata.get("risk_flags") or [])
    score -= 5_000 if metadata.get("segment_risk") else 0
    return score


def _candidate_sort_key(row: dict) -> tuple:
    return (
        row.get("source") != "mandatory_employer",
        -_selection_score(row),
        str(row.get("trade_name") or row.get("legal_name") or "").casefold(),
        str(row.get("source_external_id") or ""),
    )


def _selected_row(row: dict, *, name: str, quality: dict,
                  mandatory_row: bool) -> dict:
    selected = dict(row)
    metadata = dict(selected.get("metadata") or {})
    metadata["master_selection"] = {
        "status": "candidate_selected",
        "selected": True,
        "score": _selection_score(selected),
        "requires_employer_verification": (
            metadata.get("employer_evidence_level") != "proven"),
        "dedup_key": f"name:{name}",
        "hiring_gate_passed": False,
        "population_quality_lane": quality["proposed_lane"],
        "population_quality_evidence": quality["evidence"],
        "mandatory_quarantine_override": bool(
            mandatory_row and quality["proposed_lane"] == "quarantine"),
    }
    selected["metadata"] = metadata
    return selected


def select_employers(reservoir: list[dict], *, limit: int = 10000,
                     reservoir_min: int = 15000) -> tuple[list[dict], dict]:
    """Strictly filter, deduplicate and rank source-backed employer candidates."""
    if len(reservoir) < reservoir_min:
        raise RuntimeError(
            f"employer reservoir has {len(reservoir)} rows; need at least {reservoir_min}")
    mandatory = [row for row in reservoir if row.get("source") == "mandatory_employer"]
    mandatory_ids = {str(row.get("source_external_id") or "") for row in mandatory}
    if len(mandatory_ids) != 15:
        raise RuntimeError(f"mandatory employer seed is incomplete: {len(mandatory_ids)}/15")

    candidates = sorted(reservoir, key=_candidate_sort_key)
    selected_pool: list[dict] = []
    seen_source_ids: set[tuple[str, str]] = set()
    seen_names: set[str] = set()
    seen_domains: set[str] = set()
    deduplicated = risk_excluded = invalid = mandatory_quarantine_overrides = 0
    hard_quarantine_rules: dict[str, int] = {}
    for raw in candidates:
        row = dict(raw)
        metadata = dict(row.get("metadata") or {})
        source = str(row.get("source") or "")
        external_id = str(row.get("source_external_id") or "")
        name = company_db.normalize_company_name(
            row.get("trade_name") or row.get("legal_name"))
        domain = company_db.normalize_domain(row.get("domain"))
        mandatory_row = source == "mandatory_employer"
        if (not source or not external_id or not name
                or (not mandatory_row
                    and str(row.get("country") or "US").upper() != "US")):
            invalid += 1
            continue
        quality = classify_employer_record(row)
        if not mandatory_row and quality["proposed_lane"] == "quarantine":
            risk_excluded += 1
            for evidence in quality["evidence"]:
                if evidence["proposed_lane"] == "quarantine":
                    rule = str(evidence["rule"])
                    hard_quarantine_rules[rule] = hard_quarantine_rules.get(rule, 0) + 1
            continue
        mandatory_quarantine_overrides += int(
            mandatory_row and quality["proposed_lane"] == "quarantine")
        identity = (source, external_id)
        if (identity in seen_source_ids or name in seen_names
                or (domain and domain in seen_domains)):
            deduplicated += 1
            continue
        seen_source_ids.add(identity)
        seen_names.add(name)
        if domain:
            seen_domains.add(domain)
        selected_pool.append(_selected_row(
            row, name=name, quality=quality, mandatory_row=mandatory_row))

    if len(selected_pool) < limit:
        raise RuntimeError(
            f"strict employer gates produced only {len(selected_pool)} unique candidates; "
            f"need {limit}")
    selected = selected_pool[:limit]
    by_source: dict[str, int] = {}
    by_segment: dict[str, int] = {}
    verification_required = 0
    evidence_counts = {"proven": 0, "activity_backed": 0, "candidate": 0}
    for row in selected:
        source = str(row["source"])
        segment = str((row.get("metadata") or {}).get("employer_segment") or "general")
        by_source[source] = by_source.get(source, 0) + 1
        by_segment[segment] = by_segment.get(segment, 0) + 1
        verification_required += int(bool(
            (row.get("metadata") or {}).get("master_selection", {}).get(
                "requires_employer_verification")))
        evidence_level = str((row.get("metadata") or {}).get(
            "employer_evidence_level") or "candidate")
        evidence_counts[evidence_level] = evidence_counts.get(evidence_level, 0) + 1
    diagnostics = {
        "reservoir_candidates": len(reservoir),
        "eligible_unique": len(selected_pool),
        "deduplicated": deduplicated,
        "risk_excluded": risk_excluded,
        "hard_quarantine_excluded": risk_excluded,
        "hard_quarantine_rules": dict(sorted(hard_quarantine_rules.items())),
        "mandatory_quarantine_overrides": mandatory_quarantine_overrides,
        "invalid": invalid,
        "verification_required": verification_required,
        "employer_evidence": evidence_counts,
        "hiring_gate_accepted": 0,
        "by_source": by_source,
        "by_segment": by_segment,
    }
    return selected, diagnostics


def load_stored_reservoir() -> list[dict]:
    """Read all already-saved employer-source rows; never inspect vacancy tables."""
    with company_db.conn() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("""
              SELECT id,source,source_external_id,external_ids,source_url,
                source_observed_at,legal_name,trade_name,canonical_name,domain,
                careers_url,country,states,industry,naics,employee_size,ats,ats_slug,
                ats_url,remote_supported,typical_roles,discovery_confidence,
                domain_confidence,careers_confidence,status,match_reason,
                matched_catalog_company_key,provenance,metadata
              FROM company_discovery
              WHERE source=ANY(%s)
              ORDER BY source,source_external_id
            """, (list(_SOURCE_PRIORITY),))
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _active_source_identities() -> set[tuple[str, str]]:
    with company_db.conn() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("""
              SELECT c.source,c.source_external_id
              FROM company_employer_master m
              JOIN company_discovery c ON c.id=m.company_id
              WHERE m.in_target_population
            """)
            return {(str(source), str(external_id))
                    for source, external_id in cursor.fetchall()}


def reconcile_stored_population(*, limit: int = 10_000,
                                reservoir_min: int = 15_000,
                                apply: bool = False) -> dict:
    """Plan or atomically activate a replacement set from saved source records.

    Dry-run is the default.  Applying may add missing master rows, then switches the
    active flag using ``set_target_population``; historical master rows are retained.
    """
    reservoir = load_stored_reservoir()
    current = _active_source_identities()
    if len(reservoir) < reservoir_min:
        raise RuntimeError(
            f"stored employer reservoir has {len(reservoir)} rows; need {reservoir_min}")
    if len(current) != limit:
        raise RuntimeError(
            f"active population has {len(current)} rows; expected {limit}")
    by_identity = {
        (str(row.get("source") or ""), str(row.get("source_external_id") or "")): row
        for row in reservoir
    }
    missing = sorted(current - set(by_identity))
    if missing:
        raise RuntimeError(
            f"stored reservoir is missing {len(missing)} active source identities")

    kept: list[dict] = []
    removed_rows: list[dict] = []
    hard_rules: dict[str, int] = {}
    seen_identities: set[tuple[str, str]] = set()
    seen_names: set[str] = set()
    seen_domains: set[str] = set()
    for identity in sorted(current):
        row = dict(by_identity[identity])
        source = identity[0]
        name = company_db.normalize_company_name(
            row.get("trade_name") or row.get("legal_name"))
        quality = classify_employer_record(row)
        mandatory_row = source == "mandatory_employer"
        if quality["proposed_lane"] == "quarantine" and not mandatory_row:
            removed_rows.append(row)
            for evidence in quality["evidence"]:
                if evidence["proposed_lane"] == "quarantine":
                    rule = str(evidence["rule"])
                    hard_rules[rule] = hard_rules.get(rule, 0) + 1
            continue
        kept.append(_selected_row(
            row, name=name, quality=quality, mandatory_row=mandatory_row))
        seen_identities.add(identity)
        if name:
            seen_names.add(name)
        if domain := company_db.normalize_domain(row.get("domain")):
            seen_domains.add(domain)

    replacements: list[dict] = []
    replacement_deduplicated = replacement_invalid = replacement_quarantined = 0
    for raw in sorted(reservoir, key=_candidate_sort_key):
        if len(kept) + len(replacements) >= limit:
            break
        row = dict(raw)
        source = str(row.get("source") or "")
        external_id = str(row.get("source_external_id") or "")
        identity = (source, external_id)
        if identity in current or identity in seen_identities:
            continue
        name = company_db.normalize_company_name(
            row.get("trade_name") or row.get("legal_name"))
        domain = company_db.normalize_domain(row.get("domain"))
        mandatory_row = source == "mandatory_employer"
        if (not source or not external_id or not name
                or (not mandatory_row
                    and str(row.get("country") or "US").upper() != "US")):
            replacement_invalid += 1
            continue
        quality = classify_employer_record(row)
        if quality["proposed_lane"] == "quarantine" and not mandatory_row:
            replacement_quarantined += 1
            continue
        if name in seen_names or (domain and domain in seen_domains):
            replacement_deduplicated += 1
            continue
        replacements.append(_selected_row(
            row, name=name, quality=quality, mandatory_row=mandatory_row))
        seen_identities.add(identity)
        seen_names.add(name)
        if domain:
            seen_domains.add(domain)

    selected = kept + replacements
    if len(selected) != limit:
        raise RuntimeError(
            f"stored reservoir supplied only {len(selected)} safe active rows; need {limit}")
    mandatory_count = sum(row.get("source") == "mandatory_employer" for row in selected)
    if mandatory_count != 15:
        raise RuntimeError(
            f"stored reconcile would retain {mandatory_count}/15 mandatory employers")
    proposed = {(str(row["source"]), str(row["source_external_id"]))
                for row in selected}
    added = sorted(proposed - current)
    removed = sorted(current - proposed)
    result = {
        "applied": False,
        "selected": len(selected),
        "current_active": len(current),
        "added": len(added),
        "removed": len(removed),
        "added_source_ids": [list(item) for item in added],
        "removed_source_ids": [list(item) for item in removed],
        "reservoir_candidates": len(reservoir),
        "kept_current": len(kept),
        "hard_quarantine_excluded": len(removed_rows),
        "hard_quarantine_rules": dict(sorted(hard_rules.items())),
        "replacement_quarantined": replacement_quarantined,
        "replacement_deduplicated": replacement_deduplicated,
        "replacement_invalid": replacement_invalid,
        "mandatory": mandatory_count,
    }
    if not apply:
        return result
    synced = 0
    for source in _SOURCE_PRIORITY:
        rows = [row for row in replacements if row["source"] == source]
        if rows:
            synced += master_db.sync_source(source, rows)
    activated = master_db.set_target_population(selected, expected=limit)
    segments = refresh_segments()
    return {**result, "applied": True, "synced": synced,
            "activated": activated, "segments": segments,
            **master_db.counts()}


def collect(*, limit: int = 10000, source_limit: int = 15000,
            min_employees: int = 500) -> dict:
    company_db.ensure_schema()
    master_db.ensure_schema()
    reservoir = fetch_employer_reservoir(
        reservoir_min=source_limit, gleif_limit=source_limit,
        everify_limit=min(3000, source_limit),
        wikidata_limit=min(5000, source_limit),
        usaspending_limit=min(2000, source_limit), min_employees=min_employees)
    selected, diagnostics = select_employers(
        reservoir, limit=limit, reservoir_min=source_limit)
    company_db.upsert_records(selected)
    for source in _SOURCE_PRIORITY:
        rows = [row for row in selected if row["source"] == source]
        if rows:
            master_db.sync_source(source, rows)
    master_db.set_target_population(selected, expected=limit)
    segments = refresh_segments()
    return {"selected": len(selected), **diagnostics,
            "segments": segments, **master_db.counts()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Curated mass-hiring employer master")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--limit", type=int, default=10000)
    collect_parser.add_argument("--source-limit", type=int, default=15000,
                                help="minimum source-backed reservoir size")
    collect_parser.add_argument("--min-employees", type=int, default=500)
    reconcile = sub.add_parser("reconcile-stored")
    reconcile.add_argument("--limit", type=int, default=10000)
    reconcile.add_argument("--reservoir-min", type=int, default=15000)
    reconcile.add_argument("--apply", action="store_true",
                           help="activate the plan; default is read-only dry-run")
    enrich = sub.add_parser("enrich-structured")
    enrich.add_argument("--limit", type=int, default=2000)
    enrich.add_argument("--min-interval", type=float, default=0.25)
    enrich.add_argument("--replace", action="store_true")
    enrich_search = sub.add_parser("enrich-search")
    enrich_search.add_argument("--limit", type=int, default=2000)
    enrich_search.add_argument("--min-interval", type=float, default=0.25)
    registry = sub.add_parser("enrich-registry")
    registry.add_argument("--limit", type=int, default=2000)
    registry.add_argument("--min-interval", type=float, default=0.25)
    sub.add_parser("qualify")
    sub.add_parser("audit-domains")
    score = sub.add_parser("score")
    score.add_argument("--limit", type=int, default=2000)
    verify = sub.add_parser("verify-domains")
    verify.add_argument("--limit", type=int, default=2000)
    verify.add_argument("--workers", type=int, default=4)
    verify.add_argument("--min-interval", type=float, default=0.2)
    discover = sub.add_parser("discover-domains")
    discover.add_argument("--limit", type=int, default=100)
    discover.add_argument("--workers", type=int, default=4)
    discover.add_argument("--min-interval", type=float, default=0.5)
    careers = sub.add_parser("enrich-careers")
    careers.add_argument("--limit", type=int, default=2000)
    careers.add_argument("--workers", type=int, default=4)
    careers.add_argument("--min-interval", type=float, default=0.25)
    careers.add_argument("--replace", action="store_true")
    sub.add_parser("stats")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            company_db.ensure_schema()
            master_db.ensure_schema()
            result = {"initialized": True}
        elif args.command == "stats":
            result = master_db.counts()
        elif args.command == "collect":
            if args.limit < 15 or args.source_limit < args.limit or args.min_employees < 1:
                raise ValueError("invalid employer collection bounds")
            result = collect(limit=args.limit, source_limit=args.source_limit,
                             min_employees=args.min_employees)
        elif args.command == "reconcile-stored":
            if args.limit < 15 or args.reservoir_min < args.limit:
                raise ValueError("invalid stored reconcile bounds")
            result = reconcile_stored_population(
                limit=args.limit, reservoir_min=args.reservoir_min,
                apply=args.apply)
        elif args.command == "enrich-structured":
            from backend.tools.employer_identity_enrichment import enrich_structured
            if args.limit < 1 or args.min_interval < 0.1:
                raise ValueError("invalid enrichment bounds")
            company_db.ensure_schema()
            master_db.ensure_schema()
            if args.replace:
                master_db.reset_structured_enrichment()
            result = enrich_structured(limit=args.limit, min_interval=args.min_interval)
        elif args.command == "enrich-search":
            from backend.tools.employer_identity_enrichment import enrich_structured_search
            if args.limit < 1 or args.min_interval < 0.1:
                raise ValueError("invalid search enrichment bounds")
            company_db.ensure_schema()
            master_db.ensure_schema()
            result = enrich_structured_search(limit=args.limit,
                                              min_interval=args.min_interval)
        elif args.command == "enrich-registry":
            from backend.tools.employer_registry_enrichment import enrich_registry
            if args.limit < 1 or args.min_interval < 0.1:
                raise ValueError("invalid registry enrichment bounds")
            company_db.ensure_schema()
            master_db.ensure_schema()
            result = enrich_registry(limit=args.limit, min_interval=args.min_interval)
        elif args.command == "qualify":
            company_db.ensure_schema()
            master_db.ensure_schema()
            segments = refresh_segments()
            custom = master_db.classify_verified_custom_careers()
            consolidated = master_db.consolidate_verified_domains()
            result = {"segments": segments, "custom_classified": custom,
                      "consolidated": consolidated,
                      **master_db.refresh_identity_qualification()}
        elif args.command == "audit-domains":
            from backend.tools.employer_domain_verifier import audit_search_domains
            result = audit_search_domains()
        elif args.command == "score":
            from backend.tools.employer_scoring import score_employers
            if args.limit < 1:
                raise ValueError("invalid score limit")
            result = score_employers(limit=args.limit)
        elif args.command == "verify-domains":
            from backend.tools.employer_domain_verifier import verify_domains
            if args.limit < 1 or not 1 <= args.workers <= 4 or args.min_interval < 0.1:
                raise ValueError("invalid domain verification bounds")
            company_db.ensure_schema()
            master_db.ensure_schema()
            result = verify_domains(limit=args.limit, workers=args.workers,
                                    min_interval=args.min_interval)
        elif args.command == "discover-domains":
            from backend.tools.employer_domain_verifier import discover_search_domains
            if args.limit < 1 or not 1 <= args.workers <= 4 or args.min_interval < 0.25:
                raise ValueError("invalid domain discovery bounds")
            company_db.ensure_schema()
            master_db.ensure_schema()
            result = discover_search_domains(limit=args.limit, workers=args.workers,
                                             min_interval=args.min_interval)
        else:
            from backend.tools.employer_careers import enrich_verified_careers
            if args.limit < 1 or not 1 <= args.workers <= 4 or args.min_interval < 0.1:
                raise ValueError("invalid career enrichment bounds")
            company_db.ensure_schema()
            master_db.ensure_schema()
            if args.replace:
                master_db.reset_verified_careers()
            result = enrich_verified_careers(limit=args.limit, workers=args.workers,
                                             min_interval=args.min_interval)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
