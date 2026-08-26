"""Deterministic domain proposals for DOL OFLC employer candidates.

Names must match after representation-only normalization (no suffix stripping and
no fuzzy matching). A proposal additionally needs a structured address agreement:
state plus exact city, postal code, or street. The inherited domain remains an
unverified first-factor proposal and is never written to ``company_discovery.domain``
or ``domain_verified``.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.tools import company_discovery_db as company_db
from backend.tools.employer_dol_lca import parse_dol_lca_xlsx
from backend.tools.employer_official_crosswalk import address_key, exact_legal_key, state_keys

try:
    from psycopg2.extras import Json
except ModuleNotFoundError:  # pragma: no cover
    Json = None


PROVIDER = "dol_oflc_exact_location_domain_crosswalk"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _address_candidates(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = _mapping(row.get("metadata"))
    snapshot = _mapping(metadata.get("source_snapshot"))
    raw: list[Mapping[str, Any]] = []
    for owner in (metadata, snapshot):
        for key in ("employer_address", "legal_address", "headquarters_address"):
            value = owner.get(key)
            if isinstance(value, Mapping):
                raw.append(value)
    for item in snapshot.get("addresses") or []:
        if isinstance(item, Mapping) and isinstance(item.get("value"), Mapping):
            raw.append(item["value"])
    output: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for value in raw:
        states = state_keys(value.get("region"), value.get("state"),
                            value.get("state_code"), value.get("stateOrProvinceCode"))
        city = address_key(value.get("city") or value.get("locality"))
        postal = re.sub(r"[^a-z0-9]", "", str(
            value.get("postal_code") or value.get("postalCode") or value.get("zip") or ""
        ).casefold())
        lines = value.get("addressLines") or value.get("address_lines") or []
        if isinstance(lines, str):
            lines = [lines]
        street = address_key(value.get("address_line1") or value.get("street")
                             or value.get("address") or " ".join(map(str, lines)))
        signature = (tuple(sorted(states)), city, postal, street)
        if signature in seen or not states:
            continue
        seen.add(signature)
        output.append({"states": states, "city": city, "postal": postal,
                       "street": street})
    return output


def _location_agreement(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    shared_states = set(left.get("states") or []) & set(right.get("states") or [])
    if not shared_states:
        return ""
    if left.get("city") and left.get("city") == right.get("city"):
        return "state_city"
    if left.get("postal") and left.get("postal") == right.get("postal"):
        return "state_postal"
    if left.get("street") and left.get("street") == right.get("street"):
        return "state_street"
    return ""


def _structured_domain(row: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    candidate = company_db.normalize_domain(row.get("candidate_domain"))
    evidence = row.get("domain_evidence") or []
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            evidence = []
    factors = [dict(item) for item in evidence if isinstance(item, Mapping)
               and item.get("class") == "structured_corporate_source"
               and company_db.normalize_domain(
                   item.get("candidate_domain") or item.get("domain")) == candidate]
    return (candidate, factors) if candidate and factors else ("", [])


def propose_domains(dol_rows: Iterable[Mapping[str, Any]],
                    source_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build unique exact-name/location proposals without writing any state."""
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    source_count = 0
    eligible_source_identities = 0
    for row in source_rows:
        source_count += 1
        if row.get("source") == "dol_oflc_lca":
            continue
        domain, factors = _structured_domain(row)
        name_key = exact_legal_key(row.get("legal_name"))
        addresses = _address_candidates(row)
        if domain and factors and name_key:
            eligible_source_identities += 1
            enriched = dict(row)
            enriched["_proposal_domain"] = domain
            enriched["_proposal_factors"] = factors
            enriched["_proposal_addresses"] = addresses
            by_name[name_key].append(enriched)

    proposals: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    examined = 0
    for target in dol_rows:
        examined += 1
        name_key = exact_legal_key(target.get("legal_name"))
        target_addresses = _address_candidates(target)
        if not name_key:
            reasons["missing_legal_name"] += 1
            continue
        if not target_addresses:
            reasons["missing_structured_location"] += 1
            continue
        candidates = by_name.get(name_key, [])
        if not candidates:
            reasons["no_exact_name_with_domain"] += 1
            continue
        if not any(source["_proposal_addresses"] for source in candidates):
            reasons["source_missing_structured_location"] += 1
            continue
        matched: list[tuple[Mapping[str, Any], str]] = []
        for source in candidates:
            methods = sorted({
                method for left in target_addresses
                for right in source["_proposal_addresses"]
                if (method := _location_agreement(left, right))
            })
            if methods:
                matched.append((source, methods[0]))
        if not matched:
            reasons["location_mismatch"] += 1
            continue
        domains = {str(source["_proposal_domain"]) for source, _method in matched}
        if len(domains) != 1:
            reasons["conflicting_source_domains"] += 1
            continue
        domain = next(iter(domains))
        assertions = []
        for source, method in sorted(matched, key=lambda item: (
                str(item[0].get("source")), str(item[0].get("source_external_id")),
                int(item[0].get("id") or item[0].get("company_id") or 0))):
            assertions.append({
                "source_company_id": int(source.get("id") or source.get("company_id") or 0),
                "source": str(source.get("source") or ""),
                "source_external_id": str(source.get("source_external_id") or ""),
                "candidate_domain": domain,
                "location_method": method,
                "structured_factor_providers": sorted({
                    str(item.get("provider") or item.get("source_provider") or "")
                    for item in source["_proposal_factors"]
                }),
            })
        proposals.append({
            "status": "proposed", "provider": PROVIDER,
            "company_id": int(target.get("id") or target.get("company_id") or 0),
            "source_external_id": str(target.get("source_external_id") or ""),
            "legal_name": str(target.get("legal_name") or ""),
            "legal_name_key": name_key, "candidate_domain": domain,
            "source_assertions": assertions,
            "provenance": {
                "contract": "exact_normalized_name_plus_structured_location_v1",
                "target_source": "dol_oflc_lca",
                "source_observed_at": target.get("source_observed_at"),
                "proposed_at": _now(),
                "assertion": "unverified_cross_source_domain_proposal",
            },
        })
    return {"examined": examined, "source_rows": source_count,
            "eligible_source_identities": eligible_source_identities,
            "proposed": len(proposals), "no_match": examined - len(proposals),
            "reasons": dict(sorted(reasons.items())), "proposals": proposals}


def load_source_rows(*, limit: int = 25_000) -> list[dict[str, Any]]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.source,c.source_external_id,c.legal_name,c.states,c.metadata,
            m.candidate_domain,m.domain_evidence
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE c.source<>'dol_oflc_lca' AND NULLIF(m.candidate_domain,'') IS NOT NULL
            AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.domain_evidence) e
              WHERE e->>'class'='structured_corporate_source'
                AND lower(e->>'candidate_domain')=lower(m.candidate_domain))
          ORDER BY c.id LIMIT %s
        """, (max(1, min(int(limit), 50_000)),))
        return [dict(row) for row in cur.fetchall()]


def load_stored_dol_rows(*, limit: int = 15_000) -> list[dict[str, Any]]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.source_external_id,c.legal_name,c.states,c.metadata,
            c.source_observed_at,m.candidate_domain,m.domain_evidence
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE c.source='dol_oflc_lca' AND m.in_target_population
          ORDER BY c.id LIMIT %s
        """, (max(1, min(int(limit), 15_000)),))
        return [dict(row) for row in cur.fetchall()]


def apply_proposals(proposals: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Persist candidates/evidence only, revalidating all exact match inputs."""
    clean = [dict(item) for item in proposals]
    for item in clean:
        if (item.get("status") != "proposed" or item.get("provider") != PROVIDER
                or not item.get("source_external_id") or not item.get("candidate_domain")):
            raise ValueError("apply accepts only deterministic DOL domain proposals")
    updated = already_present = 0
    with company_db._cur() as cur:
        for proposal in clean:
            cur.execute("""
              SELECT c.id,c.source_external_id,c.legal_name,c.states,c.metadata,
                c.source_observed_at,m.candidate_domain,m.domain_evidence
              FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
              WHERE c.source='dol_oflc_lca' AND m.in_target_population
                AND c.source_external_id=%s FOR UPDATE OF c,m
            """, (proposal["source_external_id"],))
            target = cur.fetchone()
            if not target:
                raise RuntimeError("DOL proposal target is not active")
            target = dict(target)
            source_ids = [int(item["source_company_id"])
                          for item in proposal.get("source_assertions") or []]
            if not source_ids or any(company_id <= 0 for company_id in source_ids):
                raise RuntimeError("proposal has no persistent source identities")
            cur.execute("""
              SELECT c.id,c.source,c.source_external_id,c.legal_name,c.states,c.metadata,
                m.candidate_domain,m.domain_evidence
              FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
              WHERE c.id=ANY(%s) FOR UPDATE OF c,m
            """, (source_ids,))
            sources = [dict(row) for row in cur.fetchall()]
            fresh = propose_domains([target], sources)
            if fresh["proposed"] != 1:
                raise RuntimeError("proposal no longer passes exact name/location contract")
            current = fresh["proposals"][0]
            domain = company_db.normalize_domain(proposal["candidate_domain"])
            if current["candidate_domain"] != domain:
                raise RuntimeError("source domain changed after proposal")
            existing_domain = company_db.normalize_domain(target.get("candidate_domain"))
            if existing_domain and existing_domain != domain:
                raise RuntimeError("DOL target already has a conflicting domain proposal")
            existing = target.get("domain_evidence") or []
            if any(item.get("provider") == PROVIDER
                   and company_db.normalize_domain(item.get("candidate_domain")) == domain
                   for item in existing if isinstance(item, Mapping)):
                already_present += 1
                continue
            observed_at = _now()
            factor = {
                "class": "structured_corporate_source", "provider": PROVIDER,
                "candidate_domain": domain, "status": "proposal_not_verified",
                "assertion": "inherited_via_exact_name_and_structured_location",
                "legal_name_key": current["legal_name_key"],
                "source_assertions": current["source_assertions"],
                "observed_at": observed_at,
            }
            audit = {**current["provenance"], "candidate_domain": domain,
                     "source_assertions": current["source_assertions"],
                     "applied_at": observed_at}
            encoded_factor = Json([factor]) if Json is not None else [factor]
            encoded_audit = Json({"dol_oflc_domain_crosswalk": audit}) if Json is not None \
                else {"dol_oflc_domain_crosswalk": audit}
            cur.execute("""
              UPDATE company_employer_master SET candidate_domain=COALESCE(candidate_domain,%s),
                domain_evidence=domain_evidence || %s::jsonb,updated_at=now()
              WHERE company_id=%s AND in_target_population
            """, (domain, encoded_factor, int(target["id"])))
            if cur.rowcount != 1:
                raise RuntimeError("DOL domain proposal update failed")
            cur.execute("""
              UPDATE company_discovery SET provenance=provenance || %s::jsonb,updated_at=now()
              WHERE id=%s AND source='dol_oflc_lca'
            """, (encoded_audit, int(target["id"])))
            if cur.rowcount != 1:
                raise RuntimeError("DOL proposal provenance update failed")
            updated += 1
    return {"selected": len(clean), "updated": updated,
            "already_present": already_present}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=15_000)
    parser.add_argument("--xlsx", type=Path,
                        help="preview records from a DOL XLSX before they are stored")
    parser.add_argument("--apply", type=Path,
                        help="apply proposals from a reviewed JSON result")
    args = parser.parse_args(argv)
    if args.apply:
        payload = json.loads(args.apply.read_text())
        result = apply_proposals(payload.get("proposals") or [])
    else:
        targets = parse_dol_lca_xlsx(args.xlsx, limit=args.limit) if args.xlsx \
            else load_stored_dol_rows(limit=args.limit)
        result = propose_domains(targets, load_source_rows())
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
