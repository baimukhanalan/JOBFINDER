"""Authoritative employer identity and domain-assertion source adapters.

The adapters in this module deliberately stop before verification.  A website is
only emitted as an assertion attached to the stable identifier of the entity that
reported it to (or is represented by) the source.  Downstream code must corroborate
identity and domain assertions before marking an employer as verified.

No vacancy catalog, search engine, ATS board, or employer-master database is read
here.  Fetchers have bounded pagination, response sizes, retries, and rate limits.
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import time
import zipfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBMISSIONS_BULK_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
)
FDIC_INSTITUTIONS_URL = "https://api.fdic.gov/banks/institutions"
SAM_ENTITIES_URL = "https://api.sam.gov/entity-information/v4/entities"

USER_AGENT = (
    "JobFinder-authoritative-employer-sources/1.0 "
    "(+https://github.com/baimukhanalan/JOBFINDER)"
)
REQUEST_TIMEOUT = httpx.Timeout(45.0, connect=10.0, read=45.0, write=15.0)
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# These are useful discovery or hosting services, but never an assertion that an
# authority has linked a legal entity to its own official web domain.
_NON_OFFICIAL_DOMAIN_SUFFIXES = (
    "ashbyhq.com", "facebook.com", "glassdoor.com", "greenhouse.io",
    "icims.com", "instagram.com", "lever.co", "linktr.ee", "linkedin.com",
    "myworkdayjobs.com", "oraclecloud.com", "smartrecruiters.com",
    "successfactors.com", "wikipedia.org", "workable.com", "x.com",
    "youtube.com",
)
_HOST_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _domain_from_official_url(value: Any) -> tuple[str, str] | None:
    """Return a safe canonical URL/domain pair, without doing network I/O."""
    raw = _text(value)
    if not raw or any(char in raw for char in "\r\n\t"):
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        _ = parsed.port  # Reject malformed ports.
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    if parsed.username or parsed.password or not _HOST_RE.fullmatch(host):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    domain = host.removeprefix("www.")
    if (domain == "localhost" or domain.endswith((".local", ".internal", ".localhost"))
            or any(domain == suffix or domain.endswith(f".{suffix}")
                   for suffix in _NON_OFFICIAL_DOMAIN_SUFFIXES)):
        return None
    scheme = parsed.scheme.lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    canonical = f"{scheme}://{host}{port}{path}"
    if parsed.query:
        canonical += f"?{parsed.query}"
    return canonical, domain


def domain_assertion(
    *, provider: str, entity_id: str, value: Any, source_field: str,
    assertion_type: str, source_url: str, observed_at: str,
) -> dict[str, Any] | None:
    """Build an entity-bound domain assertion, or reject an unsafe/non-official URL."""
    provider = _text(provider)
    entity_id = _text(entity_id)
    source_field = _text(source_field)
    if not provider or not entity_id or not source_field:
        raise ValueError("provider, entity_id, and source_field are required")
    parsed = _domain_from_official_url(value)
    if parsed is None:
        return None
    url, domain = parsed
    return {
        "provider": provider,
        "entity_id": entity_id,
        "domain": domain,
        "url": url,
        "source_field": source_field,
        "assertion_type": _text(assertion_type),
        "provenance": {
            "source_url": _text(source_url),
            "observed_at": _text(observed_at),
            "source_field": source_field,
        },
    }


def _identity_node(
    *, provider: str, entity_id: str, entity_ids: Mapping[str, Any],
    legal_name: Any, aliases: Iterable[Any], assertions: Iterable[dict[str, Any] | None],
    attributes: Mapping[str, Any], source_url: str, observed_at: str,
    retrieval_method: str,
) -> dict[str, Any]:
    clean_aliases: list[str] = []
    for value in aliases:
        alias = _text(value)
        if alias and alias.casefold() != _text(legal_name).casefold() \
                and alias.casefold() not in {item.casefold() for item in clean_aliases}:
            clean_aliases.append(alias)
    clean_assertions = [item for item in assertions if item is not None]
    if any(item["entity_id"] != entity_id for item in clean_assertions):
        raise ValueError("domain assertion is not bound to this identity node")
    return {
        "provider": provider,
        "entity_id": entity_id,
        "entity_ids": {key: _text(value) for key, value in entity_ids.items() if _text(value)},
        "legal_name": _text(legal_name),
        "aliases": clean_aliases,
        "domain_assertions": clean_assertions,
        "attributes": dict(attributes),
        "provenance": {
            "provider": provider,
            "source_url": _text(source_url),
            "observed_at": _text(observed_at),
            "retrieval_method": retrieval_method,
        },
    }


def _sec_cik(value: Any) -> str:
    raw = _text(value)
    return raw.zfill(10) if raw.isdigit() and 1 <= len(raw) <= 10 else ""


def parse_sec_submission(
    payload: Any, *, observed_at: str = "", source_url: str = "",
) -> dict[str, Any] | None:
    """Parse one SEC submissions entity record and its self-reported websites."""
    if not isinstance(payload, Mapping):
        return None
    cik = _sec_cik(payload.get("cik"))
    legal_name = _text(payload.get("name"))
    if not cik or not legal_name:
        return None
    entity_id = f"sec_cik:{cik}"
    source_url = source_url or SEC_SUBMISSIONS_URL.format(cik=cik)
    observed_at = observed_at or _now()
    former_names = [
        _text(_mapping(item).get("name")) for item in _list(payload.get("formerNames"))
        if _text(_mapping(item).get("name"))
    ]
    assertions = [
        domain_assertion(
            provider="sec_edgar", entity_id=entity_id, value=payload.get(field),
            source_field=field, assertion_type="registrant_reported_website",
            source_url=source_url, observed_at=observed_at,
        )
        for field in ("website", "investorWebsite")
    ]
    tickers = [_text(item).upper() for item in _list(payload.get("tickers")) if _text(item)]
    exchanges = [_text(item) for item in _list(payload.get("exchanges")) if _text(item)]
    return _identity_node(
        provider="sec_edgar", entity_id=entity_id, entity_ids={"sec_cik": cik},
        legal_name=legal_name, aliases=former_names, assertions=assertions,
        attributes={
            "tickers": tickers,
            "exchanges": exchanges,
            "sic": _text(payload.get("sic")),
            "sic_description": _text(payload.get("sicDescription")),
            "state_of_incorporation": _text(payload.get("stateOfIncorporation")),
            "fiscal_year_end": _text(payload.get("fiscalYearEnd")),
        },
        source_url=source_url, observed_at=observed_at,
        retrieval_method="sec_submissions_json",
    )


def parse_sec_tickers(
    payload: Any, *, observed_at: str = "", source_url: str = SEC_TICKERS_URL,
) -> list[dict[str, Any]]:
    """Parse SEC ticker/CIK crosswalks; these rows intentionally assert no domain."""
    rows: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping) and isinstance(payload.get("fields"), list) \
            and isinstance(payload.get("data"), list):
        fields = [_text(field) for field in payload["fields"]]
        rows = [dict(zip(fields, row)) for row in payload["data"] if isinstance(row, list)]
    elif isinstance(payload, Mapping):
        rows = [value for value in payload.values() if isinstance(value, Mapping)]

    observed_at = observed_at or _now()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        cik = _sec_cik(row.get("cik") or row.get("cik_str"))
        name = _text(row.get("name") or row.get("title"))
        if not cik or not name:
            continue
        item = grouped.setdefault(cik, {"name": name, "tickers": [], "exchanges": []})
        ticker = _text(row.get("ticker")).upper()
        exchange = _text(row.get("exchange"))
        if ticker and ticker not in item["tickers"]:
            item["tickers"].append(ticker)
        if exchange and exchange not in item["exchanges"]:
            item["exchanges"].append(exchange)

    return [
        _identity_node(
            provider="sec_edgar", entity_id=f"sec_cik:{cik}",
            entity_ids={"sec_cik": cik}, legal_name=item["name"], aliases=[], assertions=[],
            attributes={"tickers": item["tickers"], "exchanges": item["exchanges"]},
            source_url=source_url, observed_at=observed_at,
            retrieval_method="sec_ticker_crosswalk",
        )
        for cik, item in sorted(grouped.items())
    ]


def parse_sec_submissions_zip(
    content: bytes, *, limit: int = 10_000, observed_at: str = "",
    source_url: str = SEC_SUBMISSIONS_BULK_URL, max_entries: int = 25_000,
    max_uncompressed_bytes: int = 2_000_000_000, max_entry_bytes: int = 8_000_000,
) -> list[dict[str, Any]]:
    """Parse a bounded SEC submissions archive without trusting ZIP metadata blindly."""
    if not content or limit <= 0:
        return []
    output: list[dict[str, Any]] = []
    observed_at = observed_at or _now()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise ValueError("SEC submissions archive has too many entries")
        total = sum(info.file_size for info in infos)
        if total > max_uncompressed_bytes:
            raise ValueError("SEC submissions archive exceeds uncompressed size limit")
        for info in infos:
            if len(output) >= limit:
                break
            if info.is_dir() or not re.fullmatch(r"CIK\d{10}\.json", info.filename):
                continue
            if info.file_size > max_entry_bytes:
                raise ValueError("SEC submission entry exceeds size limit")
            with archive.open(info) as stream:
                raw = stream.read(max_entry_bytes + 1)
            if len(raw) > max_entry_bytes:
                raise ValueError("SEC submission entry exceeds size limit")
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            node = parse_sec_submission(
                payload, observed_at=observed_at,
                source_url=SEC_SUBMISSIONS_URL.format(cik=info.filename[3:13]),
            )
            if node is not None:
                node["provenance"]["bulk_source_url"] = source_url
                output.append(node)
    return output


def parse_fdic_institutions(
    payload: Any, *, observed_at: str = "", source_url: str = FDIC_INSTITUTIONS_URL,
) -> list[dict[str, Any]]:
    """Parse BankFind institutions and their regulator-reported WEBADDR values."""
    if not isinstance(payload, Mapping):
        return []
    observed_at = observed_at or _now()
    meta = _mapping(payload.get("meta"))
    index = _mapping(meta.get("index"))
    dataset_timestamp = _text(index.get("createTimestamp"))
    nodes: list[dict[str, Any]] = []
    for wrapper in _list(payload.get("data")):
        row = _mapping(_mapping(wrapper).get("data") or wrapper)
        cert = _text(row.get("CERT"))
        name = _text(row.get("NAME"))
        if not cert.isdigit() or not name:
            continue
        entity_id = f"fdic_cert:{cert}"
        assertion = domain_assertion(
            provider="fdic_bankfind", entity_id=entity_id, value=row.get("WEBADDR"),
            source_field="WEBADDR", assertion_type="institution_reported_primary_website",
            source_url=source_url, observed_at=observed_at,
        )
        fed_rssd = _text(row.get("FED_RSSD"))
        node = _identity_node(
            provider="fdic_bankfind", entity_id=entity_id,
            entity_ids={"fdic_cert": cert, "fed_rssd": fed_rssd}, legal_name=name,
            aliases=[row.get("NAMEHCR")], assertions=[assertion],
            attributes={
                "active": row.get("ACTIVE"), "city": _text(row.get("CITY")),
                "state": _text(row.get("STALP")), "zip": _text(row.get("ZIP")),
                "bank_class": _text(row.get("BKCLASS")),
                "dataset_timestamp": dataset_timestamp,
            },
            source_url=source_url, observed_at=observed_at,
            retrieval_method="fdic_bankfind_api",
        )
        nodes.append(node)
    return nodes


def parse_sam_entity(
    payload: Any, *, observed_at: str = "", source_url: str = SAM_ENTITIES_URL,
) -> dict[str, Any] | None:
    """Parse one SAM.gov entity; the optional connector requires a caller API key."""
    if not isinstance(payload, Mapping):
        return None
    registration = _mapping(payload.get("entityRegistration"))
    core = _mapping(payload.get("coreData"))
    # v3/v4 responses nest entityInformation below coreData.  Keep the top-level
    # fallback for historical extracts and caller-normalized records.
    information = _mapping(core.get("entityInformation") or payload.get("entityInformation"))
    uei = _text(registration.get("ueiSAM") or payload.get("ueiSAM")).upper()
    legal_name = _text(registration.get("legalBusinessName") or payload.get("legalBusinessName"))
    if not re.fullmatch(r"[A-Z0-9]{12}", uei) or not legal_name:
        return None
    entity_id = f"sam_uei:{uei}"
    observed_at = observed_at or _now()
    assertion = domain_assertion(
        provider="sam_gov", entity_id=entity_id, value=information.get("entityURL"),
        source_field="entityInformation.entityURL",
        assertion_type="registrant_reported_entity_website", source_url=source_url,
        observed_at=observed_at,
    )
    physical = _mapping(core.get("physicalAddress"))
    return _identity_node(
        provider="sam_gov", entity_id=entity_id,
        entity_ids={"sam_uei": uei, "cage_code": registration.get("cageCode")},
        legal_name=legal_name, aliases=[registration.get("dbaName")], assertions=[assertion],
        attributes={
            "registration_status": _text(registration.get("registrationStatus")),
            "registration_expiration_date": _text(registration.get("registrationExpirationDate")),
            "physical_address": dict(physical),
        },
        source_url=source_url, observed_at=observed_at,
        retrieval_method="sam_entity_api_v4",
    )


def parse_sam_entities(
    payload: Any, *, observed_at: str = "", source_url: str = SAM_ENTITIES_URL,
) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("entityData") or payload.get("entities") or []
    return [node for row in _list(rows)
            if (node := parse_sam_entity(
                row, observed_at=observed_at, source_url=source_url)) is not None]


def _retry_delay(response: httpx.Response | None, attempt: int, cap: float = 30.0) -> float:
    value = response.headers.get("retry-after", "") if response is not None else ""
    try:
        return min(max(float(value), 0.0), cap)
    except ValueError:
        return min(0.5 * (2 ** attempt), cap)


def _request(
    client: httpx.Client, url: str, *, retries: int, sleep: Callable[[float], None],
    max_bytes: int, **kwargs: Any,
) -> httpx.Response:
    response: httpx.Response | None = None
    error: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        try:
            response = client.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
            if response.status_code not in RETRYABLE_STATUSES:
                response.raise_for_status()
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise ValueError(f"authoritative source response exceeds {max_bytes} bytes")
                if len(response.content) > max_bytes:
                    raise ValueError(f"authoritative source response exceeds {max_bytes} bytes")
                return response
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            error = exc
            response = None
        if attempt < max(0, int(retries)):
            sleep(_retry_delay(response, attempt))
    status = response.status_code if response is not None else "network"
    raise RuntimeError(f"authoritative source unavailable: {url} ({status})") from error


def _client(client: httpx.Client | None, headers: Mapping[str, str] | None = None):
    if client is not None:
        return client, False
    merged = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    merged.update(headers or {})
    return httpx.Client(headers=merged, timeout=REQUEST_TIMEOUT, follow_redirects=False), True


def fetch_sec_tickers(
    *, client: httpx.Client | None = None, retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    http, owned = _client(client)
    try:
        response = _request(http, SEC_TICKERS_URL, retries=retries, sleep=sleep,
                            max_bytes=10_000_000)
        return parse_sec_tickers(response.json(), source_url=str(response.url))
    finally:
        if owned:
            http.close()


def fetch_sec_submission(
    cik: Any, *, client: httpx.Client | None = None, retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    normalized = _sec_cik(cik)
    if not normalized:
        raise ValueError("CIK must contain 1 to 10 digits")
    http, owned = _client(client)
    try:
        url = SEC_SUBMISSIONS_URL.format(cik=normalized)
        response = _request(http, url, retries=retries, sleep=sleep, max_bytes=8_000_000)
        return parse_sec_submission(response.json(), source_url=str(response.url))
    finally:
        if owned:
            http.close()


def fetch_sec_submissions_bulk(
    *, limit: int = 10_000, client: httpx.Client | None = None, retries: int = 3,
    sleep: Callable[[float], None] = time.sleep, max_download_bytes: int = 1_000_000_000,
) -> list[dict[str, Any]]:
    http, owned = _client(client)
    try:
        response = _request(
            http, SEC_SUBMISSIONS_BULK_URL, retries=retries, sleep=sleep,
            max_bytes=max_download_bytes, headers={"Accept": "application/zip"},
        )
        return parse_sec_submissions_zip(
            response.content, limit=max(0, int(limit)), source_url=str(response.url))
    finally:
        if owned:
            http.close()


def fetch_fdic_institutions(
    *, limit: int = 10_000, active_only: bool = True, page_size: int = 1000,
    max_pages: int = 20, min_interval: float = 0.2,
    client: httpx.Client | None = None, retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch bounded BankFind pages; FDIC currently requires no API key."""
    if limit <= 0 or max_pages <= 0:
        return []
    page_size = max(1, min(int(page_size), 1000, int(limit)))
    http, owned = _client(client)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for page in range(max_pages):
            response = _request(
                http, FDIC_INSTITUTIONS_URL, retries=retries, sleep=sleep,
                max_bytes=25_000_000,
                params={
                    "fields": "CERT,NAME,NAMEHCR,WEBADDR,ACTIVE,CITY,STALP,ZIP,FED_RSSD,BKCLASS",
                    "filters": "ACTIVE:1" if active_only else "ACTIVE:[0 TO 1]",
                    "limit": page_size, "offset": page * page_size, "format": "json",
                },
            )
            payload = response.json()
            parsed = parse_fdic_institutions(payload, source_url=str(response.url))
            for node in parsed:
                if node["entity_id"] not in seen:
                    seen.add(node["entity_id"])
                    output.append(node)
                    if len(output) >= limit:
                        return output
            raw_count = len(_list(_mapping(payload).get("data")))
            total = _mapping(_mapping(payload).get("meta")).get("total")
            if raw_count < page_size or (isinstance(total, int) and len(seen) >= total):
                break
            if min_interval > 0 and page + 1 < max_pages:
                sleep(min_interval)
        return output
    finally:
        if owned:
            http.close()


def fetch_sam_entities(
    ueis: Iterable[str], *, api_key: str | None = None, batch_size: int = 100,
    max_batches: int = 100, max_pages_per_batch: int = 10,
    min_interval: float = 0.25,
    client: httpx.Client | None = None, retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch optional SAM entity batches; the secret is sent only in a header."""
    key = _text(api_key)
    if not key:
        raise RuntimeError("SAM.gov connector requires an API key")
    normalized = []
    for value in ueis:
        uei = _text(value).upper()
        if re.fullmatch(r"[A-Z0-9]{12}", uei) and uei not in normalized:
            normalized.append(uei)
    if not normalized or max_batches <= 0 or max_pages_per_batch <= 0:
        return []
    batch_size = max(1, min(int(batch_size), 100))
    http, owned = _client(client)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for batch_index, start in enumerate(range(0, len(normalized), batch_size)):
            if batch_index >= max_batches:
                break
            batch = normalized[start:start + batch_size]
            uei_filter = batch[0] if len(batch) == 1 else f"[{'~'.join(batch)}]"
            for page in range(min(max_pages_per_batch, (len(batch) + 9) // 10)):
                response = _request(
                    http, SAM_ENTITIES_URL, retries=retries, sleep=sleep,
                    max_bytes=30_000_000,
                    params={
                        "ueiSAM": uei_filter, "includeSections": "entityRegistration,coreData",
                        "page": page, "size": 10,
                    },
                    headers={"X-Api-Key": key},
                )
                payload = response.json()
                parsed = parse_sam_entities(payload, source_url=str(response.url))
                for node in parsed:
                    if node["entity_id"] not in seen:
                        seen.add(node["entity_id"])
                        output.append(node)
                has_next = bool(_text(_mapping(_mapping(payload).get("links")).get("nextLink")))
                if len(_list(_mapping(payload).get("entityData"))) < 10 or not has_next:
                    break
                more_requests = (page + 1 < min(max_pages_per_batch, (len(batch) + 9) // 10)
                                 or start + batch_size < len(normalized))
                if min_interval > 0 and more_requests:
                    sleep(min_interval)
            if min_interval > 0 and start + batch_size < len(normalized) \
                    and batch_index + 1 < max_batches:
                sleep(min_interval)
        return output
    finally:
        if owned:
            http.close()
