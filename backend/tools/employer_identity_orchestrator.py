"""Batch orchestration for authoritative employer identity evidence.

This module is intentionally persistence-agnostic.  It crosswalks an already
selected active-employer population against authority nodes, emits only proposed
evidence or quarantine conflicts, and checkpoints deterministic batches.  It does
not import an employer database and does not promote any identity or domain state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CONTRACT_VERSION = 1
SUPPORTED_PROVIDERS = frozenset({
    "sec_edgar", "fdic_bankfind", "sam_gov", "gleif_lei",
})
_ENTITY_ID_PATTERNS = {
    "sec_edgar": re.compile(r"sec_cik:\d{10}"),
    "fdic_bankfind": re.compile(r"fdic_cert:\d+"),
    "sam_gov": re.compile(r"sam_uei:[A-Z0-9]{12}"),
    "gleif_lei": re.compile(r"gleif_lei:[A-Z0-9]{20}"),
}
_MULTIPART_TLDS = frozenset({
    "co.uk", "org.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz",
    "com.br", "com.mx", "co.jp", "co.in", "com.sg", "com.hk", "co.za",
})
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def exact_legal_name_key(value: Any) -> str:
    """Normalize typography only; legal suffixes remain identity-significant."""
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _state(value: Any) -> str:
    raw = _text(value).upper()
    if raw.startswith("US-"):
        raw = raw[3:]
    if len(raw) == 2 and raw.isalpha():
        return raw
    return _STATE_NAMES.get(_text(value).casefold(), "")


def _postal(value: Any) -> str:
    match = re.search(r"\b\d{5}(?:-\d{4})?\b", _text(value))
    return match.group(0)[:5] if match else ""


def _address_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _text(value))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _address_parts(value: Any) -> tuple[set[str], set[str], set[str]]:
    """Return state, postal, and full-address evidence sets from flexible shapes."""
    states: set[str] = set()
    postals: set[str] = set()
    addresses: set[str] = set()
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, Mapping):
            for key in ("state", "region", "state_code", "stateOrProvinceCode", "STALP"):
                if code := _state(item.get(key)):
                    states.add(code)
            for key in ("zip", "postal", "postal_code", "postalCode", "zipCode", "ZIP"):
                if code := _postal(item.get(key)):
                    postals.add(code)
            ordered = [item.get(key) for key in (
                "addressLine1", "addressLine2", "line1", "line2", "street",
                "city", "region", "state", "stateOrProvinceCode", "zipCode",
                "postal_code", "postalCode", "country", "countryCode",
            )]
            combined = _address_key(" ".join(_text(part) for part in ordered if _text(part)))
            has_address_detail = any(_text(item.get(key)) for key in (
                "addressLine1", "addressLine2", "line1", "line2", "street", "city",
            ))
            if combined and has_address_detail:
                addresses.add(combined)
        else:
            text = _text(item)
            state_code = _state(text)
            postal_code = _postal(text)
            if state_code:
                states.add(state_code)
            if postal_code:
                postals.add(postal_code)
            if not state_code and not postal_code and (key := _address_key(text)):
                addresses.add(key)
    return states, postals, addresses


def employer_location(record: Mapping[str, Any]) -> dict[str, set[str]]:
    states: set[str] = set()
    postals: set[str] = set()
    addresses: set[str] = set()
    for key in ("state", "states", "address", "legal_address", "headquarters_address",
                "headquarters", "location"):
        found = _address_parts(record.get(key))
        states.update(found[0]); postals.update(found[1]); addresses.update(found[2])
    return {"states": states, "postals": postals, "addresses": addresses}


def node_location(node: Mapping[str, Any]) -> dict[str, set[str]]:
    attributes = _mapping(node.get("attributes"))
    states: set[str] = set()
    postals: set[str] = set()
    addresses: set[str] = set()
    candidates = [
        attributes,
        attributes.get("state"), attributes.get("state_of_incorporation"),
        attributes.get("states"),
        attributes.get("physical_address"), attributes.get("legal_address"),
        attributes.get("headquarters_address"), attributes.get("address"),
    ]
    for candidate in candidates:
        found = _address_parts(candidate)
        states.update(found[0]); postals.update(found[1]); addresses.update(found[2])
    return {"states": states, "postals": postals, "addresses": addresses}


def _location_match(employer: Mapping[str, set[str]], node: Mapping[str, set[str]]):
    labels = {"postals": "postal", "addresses": "address", "states": "state"}
    for kind in ("postals", "addresses", "states"):
        overlap = employer[kind] & node[kind]
        if overlap:
            return {"kind": labels[kind], "values": sorted(overlap)}
    employer_has = any(employer.values())
    node_has = any(node.values())
    if not employer_has or not node_has:
        return None
    return False


def _domain_root(value: Any) -> str:
    raw = _text(value).casefold()
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").strip(".").removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if tail in _MULTIPART_TLDS else tail


def gleif_identity_node(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adapt a GLEIF discovery record into the authority-node contract."""
    metadata = _mapping(record.get("metadata"))
    lei = _text(record.get("source_external_id") or metadata.get("lei")).upper()
    legal_name = _text(record.get("legal_name"))
    if not re.fullmatch(r"[A-Z0-9]{20}", lei) or not legal_name:
        return None
    legal_address = _mapping(metadata.get("legal_address"))
    headquarters = _mapping(metadata.get("headquarters_address"))
    return {
        "provider": "gleif_lei",
        "entity_id": f"gleif_lei:{lei}",
        "entity_ids": {"lei": lei},
        "legal_name": legal_name,
        "aliases": [_text(record.get("trade_name"))] if _text(record.get("trade_name")) else [],
        "domain_assertions": [],
        "attributes": {
            "legal_address": dict(legal_address),
            "headquarters_address": dict(headquarters),
            "states": list(record.get("states") or []),
            "entity_status": _text(metadata.get("entity_status")),
        },
        "provenance": {
            "provider": "gleif_lei", "source_url": _text(record.get("source_url")),
            "observed_at": _text(record.get("source_observed_at")),
            "retrieval_method": "gleif_lei_record",
        },
    }


def _validated_node(node: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    provider = _text(node.get("provider"))
    entity_id = _text(node.get("entity_id"))
    legal_name = _text(node.get("legal_name"))
    conflicts: list[dict[str, Any]] = []
    if provider not in SUPPORTED_PROVIDERS:
        conflicts.append({"type": "unsupported_provider", "provider": provider})
    entity_pattern = _ENTITY_ID_PATTERNS.get(provider)
    if entity_pattern is None or entity_pattern.fullmatch(entity_id) is None:
        conflicts.append({"type": "invalid_entity_id", "provider": provider,
                          "entity_id": entity_id})
    if not exact_legal_name_key(legal_name):
        conflicts.append({"type": "missing_legal_name", "provider": provider,
                          "entity_id": entity_id})
    assertions: list[dict[str, Any]] = []
    for assertion in node.get("domain_assertions") or []:
        if not isinstance(assertion, Mapping) or assertion.get("entity_id") != entity_id:
            conflicts.append({"type": "unbound_domain_assertion", "provider": provider,
                              "entity_id": entity_id})
            continue
        domain = _text(assertion.get("domain"))
        if not _domain_root(domain):
            conflicts.append({"type": "invalid_domain_assertion", "provider": provider,
                              "entity_id": entity_id})
            continue
        assertions.append(dict(assertion))
    if conflicts:
        return None, conflicts
    clean = dict(node)
    clean["provider"] = provider
    clean["entity_id"] = entity_id
    clean["legal_name"] = legal_name
    clean["domain_assertions"] = assertions
    clean["attributes"] = dict(_mapping(node.get("attributes")))
    clean["provenance"] = dict(_mapping(node.get("provenance")))
    return clean, []


def _active(record: Mapping[str, Any]) -> bool:
    if record.get("population_active") is False or record.get("active") is False:
        return False
    status = _text(record.get("population_status"))
    return not status or status.casefold() == "active"


class IdentityCrosswalk:
    """Immutable indexes used to make deterministic decisions for up to 10k rows."""

    def __init__(self, employers: Sequence[Mapping[str, Any]],
                 provider_nodes: Sequence[Mapping[str, Any]]) -> None:
        self.employers = [dict(row) for row in employers]
        self.company_id_counts: dict[str, int] = defaultdict(int)
        for employer in self.employers:
            company_id = _text(employer.get("company_id") or employer.get("id"))
            if company_id:
                self.company_id_counts[company_id] += 1
        self.nodes_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.node_conflicts_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        nodes_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for raw in provider_nodes:
            nodes_by_id[_text(raw.get("entity_id"))].append(raw)
        for entity_id, duplicates in nodes_by_id.items():
            signatures = {json.dumps(item, sort_keys=True, default=str) for item in duplicates}
            if not entity_id or len(signatures) > 1:
                for raw in duplicates:
                    key = exact_legal_name_key(raw.get("legal_name"))
                    self.node_conflicts_by_name[key].append({
                        "type": "provider_entity_id_collision", "entity_id": entity_id,
                        "provider": _text(raw.get("provider")),
                        "provenance": dict(_mapping(raw.get("provenance"))),
                    })
                continue
            node, conflicts = _validated_node(duplicates[0])
            key = exact_legal_name_key(duplicates[0].get("legal_name"))
            if node is None:
                for conflict in conflicts:
                    conflict["provenance"] = dict(_mapping(duplicates[0].get("provenance")))
                    self.node_conflicts_by_name[key].append(conflict)
            else:
                self.nodes_by_name[key].append(node)

        self.entity_companies: dict[str, set[str]] = defaultdict(set)
        for employer in self.employers:
            company_id = _text(employer.get("company_id") or employer.get("id"))
            name_key = exact_legal_name_key(employer.get("legal_name"))
            location = employer_location(employer)
            for node in self.nodes_by_name.get(name_key, []):
                if _location_match(location, node_location(node)):
                    self.entity_companies[node["entity_id"]].add(company_id)

    def decide(self, employer: Mapping[str, Any]) -> dict[str, Any]:
        company_id = _text(employer.get("company_id") or employer.get("id"))
        legal_name = _text(employer.get("legal_name"))
        name_key = exact_legal_name_key(legal_name)
        base = {
            "company_id": company_id,
            "legal_name": legal_name,
            "decision": "no_match",
            "proposed_identity_assertions": [],
            "proposed_domain_assertions": [],
            "conflicts": [],
            "provenance": {
                "contract": "authoritative_identity_crosswalk",
                "contract_version": CONTRACT_VERSION,
                "observed_at": _now(),
            },
        }
        if not company_id or not name_key:
            base["decision"] = "quarantine"
            base["conflicts"].append({"type": "invalid_employer_identity"})
            return base
        if self.company_id_counts.get(company_id, 0) > 1:
            base["decision"] = "quarantine"
            base["conflicts"].append({
                "type": "duplicate_company_id", "company_id": company_id,
            })
            return base
        if self.node_conflicts_by_name.get(name_key):
            base["decision"] = "quarantine"
            base["conflicts"].extend(self.node_conflicts_by_name[name_key])
            return base

        employer_loc = employer_location(employer)
        name_candidates = self.nodes_by_name.get(name_key, [])
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        rejected: list[dict[str, Any]] = []
        for node in name_candidates:
            location = _location_match(employer_loc, node_location(node))
            if isinstance(location, dict):
                matched.append((node, location))
            elif location is False:
                rejected.append({
                    "type": "location_conflict", "provider": node["provider"],
                    "entity_id": node["entity_id"],
                    "provenance": dict(node["provenance"]),
                })
            else:
                rejected.append({
                    "type": "insufficient_location_corroboration",
                    "provider": node["provider"], "entity_id": node["entity_id"],
                    "provenance": dict(node["provenance"]),
                })
        if not matched:
            if name_candidates:
                base["decision"] = "quarantine"
                base["conflicts"].extend(rejected)
            return base

        provider_ids: dict[str, set[str]] = defaultdict(set)
        for node, _location in matched:
            provider_ids[node["provider"]].add(node["entity_id"])
            owners = self.entity_companies.get(node["entity_id"], set())
            if len(owners) > 1:
                base["conflicts"].append({
                    "type": "entity_id_shared_by_employers", "provider": node["provider"],
                    "entity_id": node["entity_id"], "company_ids": sorted(owners),
                    "provenance": dict(node["provenance"]),
                })
        for provider, entity_ids in provider_ids.items():
            if len(entity_ids) > 1:
                base["conflicts"].append({
                    "type": "ambiguous_provider_identity", "provider": provider,
                    "entity_ids": sorted(entity_ids),
                })

        assertions: list[dict[str, Any]] = []
        identity_evidence: list[dict[str, Any]] = []
        for node, location in matched:
            identity_evidence.append({
                "provider": node["provider"], "entity_id": node["entity_id"],
                "entity_ids": dict(_mapping(node.get("entity_ids"))),
                "legal_name_match": {
                    "rule": "exact_normalized_legal_name",
                    "employer": legal_name, "provider": node["legal_name"],
                    "normalized": name_key,
                },
                "location_match": location,
                "provenance": dict(node["provenance"]),
            })
            for assertion in node["domain_assertions"]:
                proposed = dict(assertion)
                proposed["proposal_state"] = "proposed"
                assertions.append(proposed)
        domain_roots = {_domain_root(item["domain"]) for item in assertions}
        if len(domain_roots) > 1:
            base["conflicts"].append({
                "type": "domain_assertion_conflict", "domains": sorted(domain_roots),
                "entity_ids": sorted({item["entity_id"] for item in assertions}),
            })
        if base["conflicts"]:
            base["decision"] = "quarantine"
            # Preserve the evidence that caused quarantine, but never surface it as
            # an accepted proposal.
            base["provenance"]["quarantined_identity_evidence"] = identity_evidence
            base["provenance"]["quarantined_domain_evidence"] = assertions
            return base
        base["decision"] = "proposed"
        base["proposed_identity_assertions"] = identity_evidence
        base["proposed_domain_assertions"] = assertions
        return base


def _fingerprint(values: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _stats(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "processed": len(results),
        "proposed": sum(item.get("decision") == "proposed" for item in results),
        "quarantined": sum(item.get("decision") == "quarantine" for item in results),
        "no_match": sum(item.get("decision") == "no_match" for item in results),
    }


def _write_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run_identity_batches(
    employers: Sequence[Mapping[str, Any]], provider_nodes: Sequence[Mapping[str, Any]], *,
    checkpoint_path: str | Path | None = None, batch_size: int = 250,
    max_records: int = 10_000, max_batches: int | None = None, resume: bool = True,
) -> dict[str, Any]:
    """Crosswalk active employers with atomic checkpoint/resume after every batch."""
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if max_records < 1 or max_records > 10_000:
        raise ValueError("max_records must be between 1 and 10000")
    active = [dict(row) for row in employers if _active(row)]
    selected = active[:max_records]
    employer_fingerprint = _fingerprint(selected)
    provider_fingerprint = _fingerprint(provider_nodes)
    checkpoint = Path(checkpoint_path) if checkpoint_path is not None else None
    state: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "employer_fingerprint": employer_fingerprint,
        "provider_fingerprint": provider_fingerprint,
        "batch_size": batch_size,
        "next_offset": 0,
        "completed": False,
        "input_total": len(employers),
        "active_selected": len(selected),
        "inactive_skipped": len(employers) - len(active),
        "truncated": max(0, len(active) - len(selected)),
        "results": [],
        "stats": _stats([]),
        "updated_at": _now(),
    }
    if checkpoint is not None and checkpoint.exists() and resume:
        loaded = json.loads(checkpoint.read_text(encoding="utf-8"))
        for key, expected in (
            ("contract_version", CONTRACT_VERSION),
            ("employer_fingerprint", employer_fingerprint),
            ("provider_fingerprint", provider_fingerprint),
            ("batch_size", batch_size),
        ):
            if loaded.get(key) != expected:
                raise ValueError(f"checkpoint mismatch: {key}")
        next_offset = loaded.get("next_offset")
        if (not isinstance(next_offset, int) or next_offset < 0
                or next_offset > len(selected)
                or next_offset != len(loaded.get("results") or [])):
            raise ValueError("checkpoint is internally inconsistent")
        if bool(loaded.get("completed")) != (next_offset == len(selected)):
            raise ValueError("checkpoint completion state is inconsistent")
        state = loaded

    crosswalk = IdentityCrosswalk(selected, provider_nodes)
    if not selected:
        state["completed"] = True
        if checkpoint is not None:
            _write_checkpoint(checkpoint, state)
    batches = 0
    while state["next_offset"] < len(selected):
        if max_batches is not None and batches >= max(0, int(max_batches)):
            break
        start = int(state["next_offset"])
        stop = min(start + batch_size, len(selected))
        batch_results = [crosswalk.decide(row) for row in selected[start:stop]]
        state["results"].extend(batch_results)
        state["next_offset"] = stop
        state["completed"] = stop >= len(selected)
        state["stats"] = _stats(state["results"])
        state["updated_at"] = _now()
        if checkpoint is not None:
            _write_checkpoint(checkpoint, state)
        batches += 1
    state["batches_this_run"] = batches
    state["stats"] = _stats(state["results"])
    state["updated_at"] = _now()
    if checkpoint is not None:
        _write_checkpoint(checkpoint, state)
    return state
