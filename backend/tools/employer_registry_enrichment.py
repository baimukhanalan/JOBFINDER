"""Exact-name legal-registry enrichment for employer headquarters evidence."""
from __future__ import annotations

import time

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_master_db as master_db
from backend.tools.company_sources import GLEIF_LEI_RECORDS_URL
from backend.tools.employer_sources import USER_AGENT


def _entity_names(entity: dict) -> set[str]:
    names = [((entity.get("legalName") or {}).get("name") or "")]
    names.extend(str(item.get("name") or "") for item in entity.get("otherNames") or [])
    return {company_db.normalize_company_name(name) for name in names if name}


def _headquarters(entity: dict) -> tuple[str, str]:
    address = entity.get("headquartersAddress") or entity.get("legalAddress") or {}
    city = str(address.get("city") or "").strip()
    region = str(address.get("region") or "").strip()
    if region.startswith("US-"):
        region = region[3:]
    country = str(address.get("country") or "").strip().upper()
    return ", ".join(part for part in (city, region) if part), country


def enrich_registry(*, limit: int = 2000, min_interval: float = 0.25,
                    checkpoint: int = 50, client: httpx.Client | None = None) -> dict:
    rows = master_db.list_registry_candidates(limit=limit)
    owned = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0),
                                    headers={"User-Agent": USER_AGENT})
    pending: list[dict] = []
    matched = updated = errors = ambiguous = 0
    try:
        for row in rows:
            query = str(row.get("legal_name") or row.get("brand_name") or "").strip()
            try:
                response = client.get(GLEIF_LEI_RECORDS_URL, params={
                    "filter[entity.names]": query,
                    "filter[entity.legalAddress.country]": "US",
                    "filter[entity.status]": "ACTIVE",
                    "page[size]": 5,
                })
                response.raise_for_status()
                items = response.json().get("data") or []
            except (httpx.HTTPError, ValueError, AttributeError):
                errors += 1
                if min_interval:
                    time.sleep(min_interval)
                continue
            record_names = {
                company_db.normalize_company_name(row.get(field))
                for field in ("brand_name", "legal_name", "trade_name") if row.get(field)
            }
            exact = []
            for item in items:
                attrs = item.get("attributes") or {}
                entity = attrs.get("entity") or {}
                if _entity_names(entity) & record_names:
                    exact.append((str(attrs.get("lei") or item.get("id") or ""), entity))
            if len(exact) == 1:
                lei, entity = exact[0]
                headquarters, country = _headquarters(entity)
                if headquarters:
                    pending.append({
                        "company_id": row["company_id"], "headquarters": headquarters,
                        "headquarters_country": country or "US", "identity_confidence": 0.64,
                        "qualification_evidence": {"gleif_entity": {
                            "lei": lei, "exact_name_match": True,
                            "headquarters": headquarters, "country": country or "US",
                        }},
                    })
                    matched += 1
            elif len(exact) > 1:
                ambiguous += 1
            if len(pending) >= max(1, int(checkpoint)):
                updated += master_db.update_registry_evidence(pending)
                pending = []
            if min_interval:
                time.sleep(min_interval)
        updated += master_db.update_registry_evidence(pending)
        return {"selected": len(rows), "matched": matched, "updated": updated,
                "ambiguous": ambiguous, "errors": errors}
    finally:
        if owned:
            client.close()
