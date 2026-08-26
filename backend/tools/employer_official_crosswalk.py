"""Exact legal-name + location crosswalk to official entity identifiers.

No proposal is made from a name alone.  This module is intentionally independent
from domain verification and never writes ``domain_verified``.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_authoritative_sources as official


PROVIDER_KEYS = {"sec_edgar": "sec_cik", "fdic_bankfind": "fdic_cert",
                 "irs_exempt_org_bmf": "irs_ein"}
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
STATE_CODES = set(US_STATES.values())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def exact_legal_key(value: Any) -> str:
    """Normalize representation only; legal suffixes and all words remain."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def address_key(value: Any) -> str:
    if isinstance(value, Mapping):
        value = " ".join(str(part) for part in value.values() if part)
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    replacements = {"street": "st", "avenue": "ave", "road": "rd",
                    "boulevard": "blvd", "suite": "ste"}
    words = re.findall(r"[a-z0-9]+", text)
    return " ".join(replacements.get(word, word) for word in words)


def state_keys(*values: Any) -> set[str]:
    result: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            result.update(state_keys(*value))
            continue
        text = str(value or "")
        for token in re.findall(r"\b[A-Za-z]{2}\b", text):
            if token.upper() in STATE_CODES:
                result.add(token.upper())
        lowered = text.casefold()
        for name, code in US_STATES.items():
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                result.add(code)
    return result


def active_rows(*, limit: int = 10_000) -> list[dict]:
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.legal_name,c.states,c.external_ids,c.provenance,
                 m.headquarters,m.headquarters_country
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population ORDER BY c.id LIMIT %s
        """, (max(1, min(int(limit), 10_000)),))
        return [dict(row) for row in cur.fetchall()]


def _active_location(row: Mapping[str, Any]) -> tuple[set[str], str]:
    states = state_keys(row.get("states"), row.get("headquarters"))
    address = address_key(row.get("headquarters"))
    return states, address


def official_record(*, provider: str, entity_id: str, legal_name: str,
                    state: str = "", address: Any = "", source_url: str,
                    source_date: str = "", observed_at: str = "") -> dict:
    key = PROVIDER_KEYS.get(provider)
    prefix = {"sec_edgar": "sec_cik:", "fdic_bankfind": "fdic_cert:",
              "irs_exempt_org_bmf": "irs_ein:"}.get(provider, "")
    if not key or not entity_id.startswith(prefix) or not exact_legal_key(legal_name):
        raise ValueError("official record needs a supported provider, ID and legal name")
    return {"provider": provider, "id_key": key, "entity_id": entity_id,
            "official_id": entity_id.split(":", 1)[1], "legal_name": legal_name,
            "legal_name_key": exact_legal_key(legal_name),
            "states": sorted(state_keys(state, address)), "address_key": address_key(address),
            "address": address, "provenance": {"source_url": source_url,
                "source_date": source_date or None, "observed_at": observed_at or _now()}}


def fdic_records(nodes: Iterable[Mapping[str, Any]]) -> list[dict]:
    output = []
    for node in nodes:
        attrs = node.get("attributes") or {}
        output.append(official_record(
            provider="fdic_bankfind", entity_id=str(node.get("entity_id") or ""),
            legal_name=str(node.get("legal_name") or ""), state=str(attrs.get("state") or ""),
            address={"city": attrs.get("city"), "state": attrs.get("state"),
                     "zip": attrs.get("zip")},
            source_url=str((node.get("provenance") or {}).get("source_url") or ""),
            source_date=str(attrs.get("dataset_timestamp") or ""),
            observed_at=str((node.get("provenance") or {}).get("observed_at") or "")))
    return output


def sec_submission_records(payloads: Iterable[Mapping[str, Any]], *,
                           source_url: str, observed_at: str = "") -> list[dict]:
    output = []
    for payload in payloads:
        cik = str(payload.get("cik") or "")
        cik = cik.zfill(10) if cik.isdigit() else ""
        business = (payload.get("addresses") or {}).get("business") or {}
        if not cik or not payload.get("name"):
            continue
        output.append(official_record(
            provider="sec_edgar", entity_id=f"sec_cik:{cik}",
            legal_name=str(payload["name"]),
            state=str(business.get("stateOrCountry") or ""), address=business,
            source_url=source_url, observed_at=observed_at))
    return output


def sec_submission_zip_records(source: bytes | Path, *, source_url: str,
                               observed_at: str = "", limit: int = 25_000,
                               max_entries: int = 25_000,
                               max_uncompressed_bytes: int = 2_000_000_000,
                               max_entry_bytes: int = 8_000_000) -> list[dict]:
    """Read the official submissions full list with ZIP-bomb and row bounds."""
    archive_source = io.BytesIO(source) if isinstance(source, bytes) else source
    payloads = []
    with zipfile.ZipFile(archive_source) as archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise ValueError("SEC submissions archive has too many entries")
        if sum(info.file_size for info in infos) > max_uncompressed_bytes:
            raise ValueError("SEC submissions archive exceeds uncompressed size limit")
        for info in infos:
            if len(payloads) >= max(0, min(int(limit), max_entries)):
                break
            if info.is_dir() or not re.fullmatch(r"CIK\d{10}\.json", info.filename):
                continue
            if info.file_size > max_entry_bytes:
                raise ValueError("SEC submission entry exceeds size limit")
            raw = archive.read(info)
            if len(raw) > max_entry_bytes:
                raise ValueError("SEC submission entry exceeds size limit")
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping):
                payloads.append(payload)
    return sec_submission_records(payloads, source_url=source_url,
                                  observed_at=observed_at)


def irs_bmf_records(content: bytes | str, *, source_url: str,
                    observed_at: str = "", max_rows: int = 2_000_000) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    output = []
    for number, raw in enumerate(csv.DictReader(io.StringIO(text)), 1):
        if number > max_rows:
            raise ValueError("IRS BMF row limit exceeded")
        row = {str(key).upper(): value for key, value in raw.items()}
        ein = re.sub(r"\D", "", str(row.get("EIN") or ""))
        if len(ein) != 9 or not row.get("NAME"):
            continue
        output.append(official_record(
            provider="irs_exempt_org_bmf", entity_id=f"irs_ein:{ein}",
            legal_name=str(row["NAME"]), state=str(row.get("STATE") or ""),
            address={key.lower(): row.get(key) for key in ("STREET", "CITY", "STATE", "ZIP")
                     if row.get(key)}, source_url=source_url, observed_at=observed_at))
    return output


def propose_crosswalk(active: Iterable[Mapping[str, Any]],
                      records: Iterable[Mapping[str, Any]], *, provider: str) -> dict:
    if provider not in PROVIDER_KEYS:
        raise ValueError("unsupported crosswalk provider")
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("provider") != provider:
            raise ValueError("official record provider mismatch")
        by_name[str(record.get("legal_name_key") or "")].append(record)
    proposals = []
    reasons = Counter()
    examined = 0
    for row in active:
        examined += 1
        name_key = exact_legal_key(row.get("legal_name"))
        states, active_address = _active_location(row)
        if not name_key:
            reasons["missing_legal_name"] += 1
            continue
        if not states and not active_address:
            reasons["no_location"] += 1
            continue
        candidates = by_name.get(name_key, [])
        if not candidates:
            reasons["no_exact_legal_name"] += 1
            continue
        location_matches = []
        for candidate in candidates:
            candidate_states = set(candidate.get("states") or [])
            candidate_address = str(candidate.get("address_key") or "")
            address_match = bool(active_address and candidate_address
                                 and active_address == candidate_address)
            state_match = bool(states and candidate_states and states & candidate_states)
            if address_match or state_match:
                location_matches.append((candidate, "exact_address" if address_match else "state"))
        if len(location_matches) != 1:
            reasons["ambiguous_location" if location_matches else "location_mismatch"] += 1
            continue
        candidate, location_method = location_matches[0]
        existing = {str(k): str(v) for k, v in (row.get("external_ids") or {}).items()}
        id_key = PROVIDER_KEYS[provider]
        if existing.get(id_key) and existing[id_key] != candidate["official_id"]:
            reasons["existing_id_conflict"] += 1
            continue
        if existing.get(id_key) == candidate["official_id"]:
            reasons["already_linked"] += 1
            continue
        proposals.append({
            "status": "proposed", "company_id": int(row["id"]), "provider": provider,
            "id_key": id_key, "official_id": candidate["official_id"],
            "entity_id": candidate["entity_id"], "legal_name": row.get("legal_name"),
            "official_legal_name": candidate["legal_name"],
            "legal_name_key": name_key, "location_method": location_method,
            "matched_states": sorted(states & set(candidate.get("states") or [])),
            "active_address_key": active_address,
            "official_address_key": candidate.get("address_key") or "",
            "provenance": candidate["provenance"],
        })
    claimed = Counter(proposal["official_id"] for proposal in proposals)
    duplicate_claims = {official_id for official_id, count in claimed.items() if count > 1}
    if duplicate_claims:
        rejected = [proposal for proposal in proposals
                    if proposal["official_id"] in duplicate_claims]
        reasons["duplicate_active_claim"] += len(rejected)
        proposals = [proposal for proposal in proposals
                     if proposal["official_id"] not in duplicate_claims]
    return {"provider": provider, "examined": examined,
            "official_records": sum(len(items) for items in by_name.values()),
            "proposed": len(proposals), "no_match": examined - len(proposals),
            "reasons": dict(sorted(reasons.items())), "proposals": proposals}


def apply_proposals(proposals: list[Mapping[str, Any]]) -> dict:
    """Apply only precomputed proposals; domain verification is out of scope."""
    clean = []
    for proposal in proposals:
        provider = str(proposal.get("provider") or "")
        if proposal.get("status") != "proposed" or PROVIDER_KEYS.get(provider) != proposal.get("id_key"):
            raise ValueError("apply accepts only valid proposed official IDs")
        clean.append(dict(proposal))
    if not clean:
        return {"selected": 0, "updated": 0}
    company_ids = [int(item["company_id"]) for item in clean]
    with company_db._cur(False) as cur:
        cur.execute("""
          SELECT c.id,c.legal_name,c.states,c.external_ids,m.headquarters
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population AND c.id=ANY(%s) FOR UPDATE
        """, (company_ids,))
        rows = {int(row[0]): {"legal_name": row[1], "states": row[2],
                             "external_ids": row[3], "headquarters": row[4]}
                for row in cur.fetchall()}
        if set(rows) != set(company_ids):
            raise RuntimeError("proposal preflight could not lock every active company")
        for proposal in clean:
            company_id = int(proposal["company_id"])
            row = rows[company_id]
            states, address = _active_location(row)
            if exact_legal_key(row["legal_name"]) != proposal["legal_name_key"]:
                raise RuntimeError("legal name changed after proposal")
            if proposal["location_method"] == "state" and not (
                    states & set(proposal.get("matched_states") or [])):
                raise RuntimeError("state evidence changed after proposal")
            if proposal["location_method"] == "exact_address" and address != proposal["active_address_key"]:
                raise RuntimeError("address evidence changed after proposal")
            existing = row.get("external_ids") or {}
            key = proposal["id_key"]
            if existing.get(key) and str(existing[key]) != str(proposal["official_id"]):
                raise RuntimeError("official ID conflicts with existing external_ids")
            evidence = json.dumps({"official_id_crosswalk": {proposal["provider"]: {
                **proposal, "applied_at": _now(),
                "assertion": "exact_legal_name_and_unique_location",
            }}})
            cur.execute("""
              UPDATE company_discovery SET
                external_ids=jsonb_set(external_ids,%s,%s::jsonb,TRUE),
                provenance=provenance || %s::jsonb,updated_at=now()
              WHERE id=%s
            """, ([key], json.dumps(str(proposal["official_id"])), evidence, company_id))
            if cur.rowcount != 1:
                raise RuntimeError("proposal update failed")
    return {"selected": len(clean), "updated": len(clean)}


def live_fdic_proposal(*, limit_active: int = 10_000,
                       limit_official: int = 10_000) -> dict:
    nodes = official.fetch_fdic_institutions(limit=limit_official, active_only=True,
                                             page_size=1000, max_pages=10,
                                             min_interval=0.1)
    return propose_crosswalk(active_rows(limit=limit_active), fdic_records(nodes),
                             provider="fdic_bankfind")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    proposal = sub.add_parser("proposal")
    proposal.add_argument("--provider", choices=("fdic_bankfind", "sec_edgar",
                                                   "irs_exempt_org_bmf"), required=True)
    proposal.add_argument("--input", type=Path)
    proposal.add_argument("--source-url",
                          help="official download URL for a local SEC/IRS full-list extract")
    proposal.add_argument("--limit-active", type=int, default=10_000)
    apply = sub.add_parser("apply"); apply.add_argument("--proposal", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "apply":
        payload = json.loads(args.proposal.read_text())
        result = apply_proposals(payload.get("proposals") or [])
    elif args.provider == "fdic_bankfind" and args.input is None:
        result = live_fdic_proposal(limit_active=args.limit_active)
    else:
        if args.input is None:
            raise ValueError("--input official full-list extract is required for SEC/IRS")
        if not args.source_url or not args.source_url.startswith("https://"):
            raise ValueError("--source-url official HTTPS provenance is required")
        active = active_rows(limit=args.limit_active)
        if args.provider == "irs_exempt_org_bmf":
            records = irs_bmf_records(args.input.read_bytes(), source_url=args.source_url)
        else:
            records = sec_submission_zip_records(args.input, source_url=args.source_url)
        result = propose_crosswalk(active, records, provider=args.provider)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
