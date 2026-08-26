"""Independent US company sources for the company-discovery pipeline.

This module deliberately does not import the vacancy catalog, ATS targets, or job
aggregators.  It only turns records from authoritative company/recipient registries
into a common shape that later discovery stages can enrich with domains and careers
pages.
"""
from __future__ import annotations

import os
import io
import json
import time
import zipfile
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBMISSIONS_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
USASPENDING_RECIPIENTS_URL = "https://api.usaspending.gov/api/v2/recipient/"
SAM_ENTITIES_URL = "https://api.sam.gov/entity-information/v3/entities"
GLEIF_LEI_RECORDS_URL = "https://api.gleif.org/api/v1/lei-records"

# SEC asks automated clients to identify themselves.  Deployments should override
# this with SEC_USER_AGENT and include a monitored contact address.
DEFAULT_SEC_USER_AGENT = (
    "JobFinder company-discovery/1.0 "
    "(https://github.com/baimukhanalan/JOBFINDER)"
)
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU", "AS", "MP",
})

RECORD_FIELDS = (
    "source",
    "source_external_id",
    "source_url",
    "source_observed_at",
    "legal_name",
    "trade_name",
    "domain",
    "careers_url",
    "country",
    "states",
    "industry",
    "naics",
    "employee_size",
    "ats",
    "ats_slug",
    "ats_url",
    "metadata",
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _domain(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _country(value: Any, default: str = "US") -> str:
    code = _text(value).upper()
    if not code:
        return default
    if code in US_STATE_CODES or code in {"USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
        return "US"
    return code


def _states(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            if isinstance(item, Mapping):
                item = _first(item, "state", "stateCode", "state_code")
            state = _text(item).upper()
            if state and state not in out:
                out.append(state)
    return out


def company_record(
    *, source: str, source_external_id: Any, legal_name: Any,
    source_url: Any = "", source_observed_at: Any = "",
    trade_name: Any = "", domain: Any = "", careers_url: Any = "",
    country: Any = "US", states: Any = None, industry: Any = "",
    naics: Any = "", employee_size: Any = "", ats: Any = "",
    ats_slug: Any = "", ats_url: Any = "", metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable record contract used by all source adapters."""
    record = {
        "source": _text(source),
        "source_external_id": _text(source_external_id),
        "source_url": _text(source_url) or None,
        "source_observed_at": _text(source_observed_at) or None,
        "legal_name": _text(legal_name),
        "trade_name": _text(trade_name),
        "domain": _domain(domain),
        "careers_url": _text(careers_url),
        "country": _country(country),
        "states": _states(states),
        "industry": _text(industry),
        "naics": _text(naics),
        "employee_size": _text(employee_size),
        "ats": _text(ats),
        "ats_slug": _text(ats_slug),
        "ats_url": _text(ats_url),
        "metadata": dict(metadata or {}),
    }
    assert tuple(record) == RECORD_FIELDS
    return record


def parse_gleif_lei_records(payload: Any) -> list[dict[str, Any]]:
    """Parse a US GLEIF JSON:API page into the stable discovery contract."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        return []
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    golden = meta.get("goldenCopy") if isinstance(meta.get("goldenCopy"), Mapping) else {}
    observed_at = _text(golden.get("publishDate"))
    records: list[dict[str, Any]] = []
    for item in payload["data"]:
        if not isinstance(item, Mapping):
            continue
        attributes = item.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        entity = attributes.get("entity")
        entity = entity if isinstance(entity, Mapping) else {}
        legal_name = entity.get("legalName")
        legal_name = legal_name if isinstance(legal_name, Mapping) else {}
        legal_address = entity.get("legalAddress")
        legal_address = legal_address if isinstance(legal_address, Mapping) else {}
        headquarters = entity.get("headquartersAddress")
        headquarters = headquarters if isinstance(headquarters, Mapping) else {}
        lei = _text(_first(attributes, "lei") or item.get("id"))
        name = _text(legal_name.get("name"))
        country = _text(legal_address.get("country")).upper()
        if country in {"USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
            country = "US"
        category = _text(entity.get("category")).upper()
        entity_status = _text(entity.get("status")).upper()
        # Defense in depth: never trust a caller to have supplied the API filter.
        if (not lei or not name or country != "US"
                or (category and category != "GENERAL")
                or (entity_status and entity_status != "ACTIVE")):
            continue
        other_names = entity.get("otherNames")
        other_names = other_names if isinstance(other_names, list) else []
        trade_name = next(
            (_text(other.get("name")) for other in other_names
             if isinstance(other, Mapping) and _text(other.get("name"))), "")
        registration = attributes.get("registration")
        registration = registration if isinstance(registration, Mapping) else {}
        legal_form = entity.get("legalForm")
        legal_form = legal_form if isinstance(legal_form, Mapping) else {}
        registered_at = entity.get("registeredAt")
        registered_at = registered_at if isinstance(registered_at, Mapping) else {}
        links = item.get("links") if isinstance(item.get("links"), Mapping) else {}
        jurisdiction = _text(entity.get("jurisdiction"))
        states = [
            value[3:] if _text(value).upper().startswith("US-") else value
            for value in (legal_address.get("region"), headquarters.get("region"), jurisdiction)
            if value
        ]
        addresses = []
        if legal_address:
            addresses.append({"address_type": "registered", "value": dict(legal_address)})
        if headquarters:
            addresses.append({"address_type": "headquarters", "value": dict(headquarters)})
        records.append(company_record(
            source="gleif_lei", source_external_id=lei,
            source_url=_text(links.get("self")) or f"{GLEIF_LEI_RECORDS_URL}/{lei}",
            source_observed_at=observed_at or registration.get("lastUpdateDate"),
            legal_name=name, trade_name=trade_name, country=country, states=states,
            metadata={
                "lei": lei,
                "jurisdiction": jurisdiction,
                "entity_status": entity_status,
                "entity_category": category,
                "legal_form_id": _text(legal_form.get("id")),
                "registered_at": _text(registered_at.get("id")),
                "registered_as": _text(entity.get("registeredAs")),
                "legal_address": dict(legal_address),
                "headquarters_address": dict(headquarters),
                "addresses": addresses,
                "registration_status": _text(registration.get("status")),
                "last_update_date": _text(registration.get("lastUpdateDate")),
            },
        ))
    return records


def fetch_gleif_companies(
    *, limit: int = 1000, page_size: int = 200, max_pages: int = 20,
    country: str = "US", retries: int = 3, client: httpx.Client | None = None,
    sleep=time.sleep,
) -> list[dict[str, Any]]:
    """Fetch active general entities from the public GLEIF LEI Registry.

    Restricting category to GENERAL excludes funds, branches and other special-purpose
    categories, producing a materially more employer-relevant starting universe.
    """
    if limit <= 0 or max_pages <= 0:
        return []
    country = _country(country, default="US")
    size = max(1, min(int(page_size), 200))
    request_size = min(size, int(limit))
    http, owned = _client(client, {"User-Agent": DEFAULT_SEC_USER_AGENT})
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for page in range(1, max_pages + 1):
            response = None
            for attempt in range(max(0, int(retries)) + 1):
                response = http.get(
                    GLEIF_LEI_RECORDS_URL,
                    params={
                        "filter[entity.legalAddress.country]": country,
                        "filter[entity.category]": "GENERAL",
                        "filter[entity.status]": "ACTIVE",
                        "page[number]": page,
                        # Keep page size stable: changing it mid-pagination changes
                        # offsets and can repeat or skip records after a duplicate.
                        "page[size]": request_size,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt < retries:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() \
                        else min(2 ** attempt, 8)
                    sleep(delay)
            assert response is not None
            response.raise_for_status()
            body = response.json()
            parsed = parse_gleif_lei_records(body)
            for record in parsed:
                lei = record["source_external_id"]
                if lei not in seen:
                    seen.add(lei)
                    records.append(record)
                    if len(records) >= limit:
                        return records
            links = body.get("links") if isinstance(body, Mapping) else {}
            if not parsed or not (links or {}).get("next"):
                break
        return records
    finally:
        if owned:
            http.close()


def parse_sec_tickers(payload: Any, limit: int = 0) -> list[dict[str, Any]]:
    """Parse SEC's company_tickers.json (mapping or list form)."""
    rows: Iterable[Any]
    if isinstance(payload, Mapping):
        rows = payload.values()
    elif isinstance(payload, list):
        rows = payload
    else:
        return []

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cik_raw = _first(row, "cik_str", "cik", "cikNumber")
        name = _first(row, "title", "name", "entityName")
        if cik_raw in (None, "") or not _text(name):
            continue
        try:
            cik = f"{int(str(cik_raw)):010d}"
        except (TypeError, ValueError):
            continue
        if cik in seen:
            continue
        seen.add(cik)
        ticker = _text(_first(row, "ticker", "tickers"))
        records.append(company_record(
            source="sec_edgar",
            source_external_id=cik,
            legal_name=name,
            metadata={"cik": cik, "ticker": ticker},
        ))
        if limit and len(records) >= limit:
            break
    return records


def parse_sec_submission(payload: Any) -> dict[str, Any] | None:
    """Parse one data.sec.gov submissions record for bounded CIK enrichment."""
    if not isinstance(payload, Mapping):
        return None
    cik_raw = _first(payload, "cik", "cik_str")
    name = _first(payload, "name", "entityName")
    if cik_raw in (None, "") or not _text(name):
        return None
    try:
        cik = f"{int(str(cik_raw)):010d}"
    except (TypeError, ValueError):
        return None
    addresses = payload.get("addresses") if isinstance(payload.get("addresses"), Mapping) else {}
    business = addresses.get("business") if isinstance(addresses.get("business"), Mapping) else {}
    mailing = addresses.get("mailing") if isinstance(addresses.get("mailing"), Mapping) else {}
    tickers = payload.get("tickers") if isinstance(payload.get("tickers"), list) else []
    exchanges = payload.get("exchanges") if isinstance(payload.get("exchanges"), list) else []
    return company_record(
        source="sec_edgar",
        source_external_id=cik,
        legal_name=name,
        domain=_first(payload, "website", "investorWebsite") or "",
        country=_country(_first(business, "stateOrCountry", "country")),
        states=[
            code for code in _states(
                business.get("stateOrCountry"), mailing.get("stateOrCountry"),
            ) if code in US_STATE_CODES
        ],
        industry=_first(payload, "sicDescription", "industry") or "",
        metadata={
            "cik": cik,
            "sic": _text(payload.get("sic")),
            "tickers": tickers,
            "exchanges": exchanges,
            "state_of_incorporation": _text(payload.get("stateOfIncorporation")),
            "fiscal_year_end": _text(payload.get("fiscalYearEnd")),
            "entity_type": _text(payload.get("entityType")),
            "website": _text(payload.get("website")),
            "investor_website": _text(payload.get("investorWebsite")),
        },
    )


def parse_sec_submissions_zip(content: bytes, limit: int = 0,
                              operating_only: bool = True) -> list[dict[str, Any]]:
    """Parse SEC's nightly submissions.zip without thousands of per-CIK requests."""
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in sorted(archive.namelist()):
            if not name.lower().endswith(".json"):
                continue
            try:
                payload = json.loads(archive.read(name))
            except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            entity_type = _text(payload.get("entityType")).casefold() \
                if isinstance(payload, Mapping) else ""
            if operating_only and entity_type and entity_type != "operating":
                continue
            record = parse_sec_submission(payload)
            if record is not None:
                records.append(record)
                if limit and len(records) >= limit:
                    break
    return records


def fetch_sec_bulk_companies(*, limit: int = 0, operating_only: bool = True,
                             client: httpx.Client | None = None,
                             user_agent: str | None = None) -> list[dict[str, Any]]:
    """Download one nightly SEC bulk archive and return normalized filer records."""
    headers = {
        "User-Agent": user_agent or os.getenv("SEC_USER_AGENT", DEFAULT_SEC_USER_AGENT),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/zip",
    }
    http, owned = _client(client, headers)
    try:
        response = http.get(SEC_SUBMISSIONS_BULK_URL, headers=headers,
                            timeout=httpx.Timeout(180.0, connect=15.0))
        response.raise_for_status()
        return parse_sec_submissions_zip(response.content, limit=limit,
                                         operating_only=operating_only)
    finally:
        if owned:
            http.close()


def _client(client: httpx.Client | None, headers: Mapping[str, str] | None = None):
    if client is not None:
        return client, False
    return httpx.Client(headers=dict(headers or {}), timeout=REQUEST_TIMEOUT), True


def fetch_sec_companies(
    *, limit: int = 100, enrich_submissions: bool = False,
    client: httpx.Client | None = None, user_agent: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch a bounded public-company sample from SEC, optionally enriching each CIK."""
    if limit <= 0:
        return []
    headers = {
        "User-Agent": user_agent or os.getenv("SEC_USER_AGENT", DEFAULT_SEC_USER_AGENT),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    }
    http, owned = _client(client, headers)
    try:
        response = http.get(SEC_TICKERS_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        records = parse_sec_tickers(response.json(), limit=limit)
        if not enrich_submissions:
            return records
        enriched: list[dict[str, Any]] = []
        for base in records:
            cik = base["source_external_id"]
            response = http.get(
                SEC_SUBMISSIONS_URL.format(cik=cik), headers=headers, timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            enriched.append(parse_sec_submission(response.json()) or base)
        return enriched
    finally:
        if owned:
            http.close()


def parse_usaspending_recipients(payload: Any) -> list[dict[str, Any]]:
    """Parse recipient category results returned by USAspending."""
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _first(row, "name", "recipient_name", "legal_business_name")
        external_id = _first(
            row, "uei", "id", "recipient_id", "code", "recipient_hash", "duns",
        )
        if not _text(name) or external_id in (None, ""):
            continue
        records.append(company_record(
            source="usaspending",
            source_external_id=external_id,
            legal_name=name,
            trade_name=_first(row, "business_name", "trade_name") or "",
            country=_first(row, "country_code", "country") or "US",
            states=_first(row, "state_code", "state") or [],
            metadata={
                "uei": _text(row.get("uei")),
                "duns": _text(row.get("duns")),
                "recipient_id": _text(row.get("id")),
                "recipient_level": _text(row.get("recipient_level")),
                "amount": row.get("amount", row.get("aggregated_amount")),
            },
        ))
    return records


def parse_usaspending_recipient_profile(payload: Any) -> dict[str, Any]:
    """Keep only identity, business classification and recipient-location fields."""
    if not isinstance(payload, Mapping):
        return {}
    location = payload.get("location")
    location = dict(location) if isinstance(location, Mapping) else {}
    business_types = payload.get("business_types")
    business_types = [str(value) for value in business_types if _text(value)] \
        if isinstance(business_types, list) else []
    return {
        "name": _text(payload.get("name")),
        "alternate_names": [str(value) for value in payload.get("alternate_names", [])
                            if _text(value)] if isinstance(payload.get("alternate_names"), list) else [],
        "uei": _text(payload.get("uei")), "duns": _text(payload.get("duns")),
        "recipient_id": _text(payload.get("recipient_id")),
        "recipient_level": _text(payload.get("recipient_level")),
        "parent_id": _text(payload.get("parent_id")),
        "parent_name": _text(payload.get("parent_name")),
        "parent_uei": _text(payload.get("parent_uei")),
        "business_types": business_types,
        "recipient_location": {
            "address_type": "recipient_location",
            "value": location,
        } if location else None,
    }


def fetch_usaspending_recipients(
    *, limit: int = 1000, page_size: int = 100, max_pages: int = 20,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch recipients with awards in USAspending's trailing 12-month window."""
    if limit <= 0 or max_pages <= 0:
        return []
    size = max(1, min(page_size, 1000))
    http, owned = _client(client, {"User-Agent": DEFAULT_SEC_USER_AGENT})
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for page in range(1, max_pages + 1):
            payload = {
                "order": "desc",
                "sort": "amount",
                "award_type": "all",
                "limit": min(size, limit - len(records)),
                "page": page,
            }
            response = http.post(
                USASPENDING_RECIPIENTS_URL, json=payload, timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
            parsed = parse_usaspending_recipients(body)
            for record in parsed:
                key = record["source_external_id"]
                if key not in seen:
                    seen.add(key)
                    records.append(record)
                    if len(records) >= limit:
                        return records
            page_meta = body.get("page_metadata", {}) if isinstance(body, Mapping) else {}
            has_next = _first(page_meta, "hasNext", "has_next", "has_next_page")
            total = page_meta.get("total") if isinstance(page_meta, Mapping) else None
            reached_total = (
                isinstance(total, int) and page * int(payload["limit"]) >= total
            )
            if has_next is False or reached_total or not parsed:
                break
        return records
    finally:
        if owned:
            http.close()


def parse_sam_entities(payload: Any) -> list[dict[str, Any]]:
    """Parse SAM.gov Entity Management API v3 results."""
    if not isinstance(payload, Mapping):
        return []
    rows = _first(payload, "entityData", "entities", "results")
    if not isinstance(rows, list):
        return []
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        registration = row.get("entityRegistration")
        registration = registration if isinstance(registration, Mapping) else row
        core = row.get("coreData") if isinstance(row.get("coreData"), Mapping) else {}
        address = core.get("physicalAddress") if isinstance(core.get("physicalAddress"), Mapping) else {}
        assertions = row.get("assertions") if isinstance(row.get("assertions"), Mapping) else {}
        goods = assertions.get("goodsAndServices") if isinstance(assertions.get("goodsAndServices"), Mapping) else {}
        naics_list = goods.get("naicsList") if isinstance(goods.get("naicsList"), list) else []
        primary_naics = ""
        if naics_list:
            first_naics = naics_list[0]
            primary_naics = _text(
                _first(first_naics, "naicsCode", "naics") if isinstance(first_naics, Mapping)
                else first_naics
            )
        uei = _first(registration, "ueiSAM", "uei", "uniqueEntityId")
        name = _first(registration, "legalBusinessName", "legal_name", "name")
        if not _text(uei) or not _text(name):
            continue
        records.append(company_record(
            source="sam_gov",
            source_external_id=uei,
            legal_name=name,
            trade_name=_first(registration, "dbaName", "doingBusinessAsName") or "",
            country=_first(address, "countryCode", "country") or "US",
            states=_first(address, "stateOrProvinceCode", "state") or [],
            naics=primary_naics,
            metadata={
                "uei": _text(uei),
                "cage_code": _text(_first(registration, "cageCode", "cage")),
                "registration_status": _text(registration.get("registrationStatus")),
                "naics_list": naics_list,
            },
        ))
    return records


def fetch_sam_companies(
    *, api_key: str | None = None, limit: int = 1000, page_size: int = 100,
    max_pages: int = 20, client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Fetch active SAM entities.  Returns no rows when no SAM API key is configured."""
    key = api_key or os.getenv("SAM_API_KEY", "")
    if not key or limit <= 0 or max_pages <= 0:
        return []
    # SAM's synchronous JSON response allows at most 10 entities per page.
    size = max(1, min(page_size, 10))
    http, owned = _client(client, {"User-Agent": DEFAULT_SEC_USER_AGENT})
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for page in range(max_pages):
            response = http.get(
                SAM_ENTITIES_URL,
                params={
                    "registrationStatus": "A",
                    "includeSections": "entityRegistration,coreData,assertions",
                    "page": page,
                    "size": min(size, limit - len(records)),
                },
                headers={"X-Api-Key": key},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
            parsed = parse_sam_entities(body)
            for record in parsed:
                uei = record["source_external_id"]
                if uei not in seen:
                    seen.add(uei)
                    records.append(record)
                    if len(records) >= limit:
                        return records
            links = body.get("links") if isinstance(body, Mapping) else None
            next_link = links.get("nextLink") if isinstance(links, Mapping) else None
            total_pages = body.get("totalPages") if isinstance(body, Mapping) else None
            if (
                not parsed
                or (isinstance(total_pages, int) and page + 1 >= total_pages)
                or (isinstance(links, Mapping) and not next_link)
            ):
                break
        return records
    finally:
        if owned:
            http.close()
