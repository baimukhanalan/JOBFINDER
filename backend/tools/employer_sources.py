"""Source-backed candidates for the curated mass-hiring employer master.

Every adapter states whether it proves employer activity or only legal identity.  A
registry row is never presented as an enriched/verified employer merely because it
has an authoritative legal-entity identifier.
"""
from __future__ import annotations

import time
import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from backend.tools.company_sources import (
    GLEIF_LEI_RECORDS_URL, US_STATE_CODES, company_record,
    fetch_usaspending_recipients, parse_gleif_lei_records,
)

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "JobFinder-employer-master/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
EVERIFY_MIRROR_URL = "https://h1btrack.com/e-verify/employers/"
_BLOCKED_DOMAIN_SUFFIXES = (
    "facebook.com", "instagram.com", "linkedin.com", "linktr.ee", "x.com",
    "wikipedia.org", "youtube.com",
)

MANDATORY_EMPLOYERS = (
    ("Amazon", "Amazon.com, Inc.", "amazon.com"),
    ("Concentrix", "Concentrix Corporation", "concentrix.com"),
    ("Foundever", "Foundever", "foundever.com"),
    ("TTEC", "TTEC Holdings, Inc.", "ttec.com"),
    ("Teleperformance", "Teleperformance SE", "teleperformance.com"),
    ("CVS Health", "CVS Health Corporation", "cvshealth.com"),
    ("UnitedHealth Group", "UnitedHealth Group Incorporated", "unitedhealthgroup.com"),
    ("JPMorgan Chase", "JPMorgan Chase & Co.", "jpmorganchase.com"),
    ("Walmart", "Walmart Inc.", "walmart.com"),
    ("Target", "Target Corporation", "target.com"),
    ("Hilton", "Hilton Worldwide Holdings Inc.", "hilton.com"),
    ("Marriott", "Marriott International, Inc.", "marriott.com"),
    ("Progressive", "The Progressive Corporation", "progressive.com"),
    ("State Farm", "State Farm Mutual Automobile Insurance Company", "statefarm.com"),
    ("Allstate", "The Allstate Corporation", "allstate.com"),
)

_SEGMENT_PATTERNS = (
    ("staffing", re.compile(
        r"\b(staffing|employment|personnel|workforce|talent solutions|human resources)\b", re.I)),
    ("government", re.compile(
        r"(^|\b)(united states|u\.s\.|department of|city of|county of|state of|government|authority|administration|district)\b", re.I)),
    ("education", re.compile(
        r"\b(university|college|school|academy|education|regents)\b", re.I)),
    ("healthcare", re.compile(
        r"\b(hospital|health|healthcare|medical|clinic|pharmacy)\b", re.I)),
    ("nonprofit", re.compile(
        r"\b(foundation|association|society|charities|charity|ministries|church)\b", re.I)),
)
_RISK_PATTERNS = (
    ("aggregate_affiliates", re.compile(
        r"\b(and (its )?(subsidiaries|affiliates)|on behalf of|collectively known as)\b", re.I)),
    ("fund_or_trust", re.compile(
        r"\b(fund|pension|investment fund|retirement trust|statutory trust)\b", re.I)),
    ("administrative_entity", re.compile(
        r"\b(payroll|shared services|administrative services)\b", re.I)),
    ("subsidiary_or_division", re.compile(
        r"\b(subsidiary|subsidiaries|affiliate|affiliates|division|regional operations)\b", re.I)),
)


def mark_employer_candidate(record: dict) -> dict:
    """Attach source strength and segment/risk labels without fabricating evidence."""
    out = dict(record)
    metadata = dict(out.get("metadata") or {})
    source = str(out.get("source") or "")
    source_labels = {
        "mandatory_employer": ("curated_candidate", "mandatory_candidate"),
        "everify_large_employer": (
            "candidate_public_mirror", "workforce_range_10000_plus"),
        "wikidata_employer": (
            "candidate_structured", "published_employee_count_candidate"),
        "usaspending": (
            "candidate_government_activity", "federal_award_recipient"),
        "gleif_lei": ("authoritative_registry", "legal_identity_only"),
    }
    source_class, employer_evidence = source_labels.get(
        source, ("candidate_other", "not_established"))
    name = str(out.get("trade_name") or out.get("legal_name") or "").strip()
    segment = next(
        (label for label, pattern in _SEGMENT_PATTERNS if pattern.search(name)), "general")
    risk_flags = [label for label, pattern in _RISK_PATTERNS if pattern.search(name)]
    if len(name) > 80 or len(name.split()) > 12:
        risk_flags.append("name_anomaly")
    metadata.update({
        "source_class": source_class,
        "employer_evidence": employer_evidence,
        "employer_candidate": True,
        "employer_segment": segment,
        "segment_risk": segment != "general",
        "risk_flags": sorted(set(risk_flags)),
        "employer_evidence_level": (
            "proven" if source in {"everify_large_employer", "wikidata_employer"}
            else "activity_backed" if source == "usaspending" else "candidate"),
    })
    out["metadata"] = metadata
    return out


class _EmployerTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._field = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        values = {str(key): str(value or "") for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "tr" and "evm-tr" in classes:
            self._row = {"states": []}
        if self._row is None:
            return
        field = ""
        if "evm-enm" in classes:
            field = "legal_name"
        elif "evm-dba" in classes:
            field = "dba"
        elif "evm-tdsz" in classes:
            field = "workforce"
        elif "evm-tddt" in classes:
            field = "enrolled"
        elif "evm-sites" in classes:
            field = "sites"
        elif "evm-stag" in classes:
            field = "state"
        if field:
            self._field = field
            self._text = []

    def handle_data(self, data) -> None:
        if self._field:
            self._text.append(str(data))

    def handle_endtag(self, tag) -> None:
        if self._row is None:
            return
        if self._field and tag in {"div", "td", "span"}:
            value = " ".join(" ".join(self._text).split())
            if self._field == "state":
                if re.fullmatch(r"[A-Z]{2}", value):
                    self._row["states"].append(value)
                elif value.startswith("+") and value[1:].isdigit():
                    self._row["additional_states"] = int(value[1:])
            elif value:
                self._row[self._field] = value
            self._field = ""
            self._text = []
        if tag == "tr":
            if self._row.get("legal_name"):
                self.rows.append(self._row)
            self._row = None


def parse_everify_employer_page(html: str, *, source_url: str,
                                observed_at: str) -> list[dict]:
    parser = _EmployerTableParser()
    parser.feed(html or "")
    records = []
    for row in parser.rows:
        legal_name = row.get("legal_name", "").strip()
        dba = re.sub(r"^DBA:\s*", "", row.get("dba", ""), flags=re.I).strip()
        workforce = row.get("workforce", "")
        if workforce != "10,000 and over":
            continue
        sites_text = row.get("sites", "0").replace(",", "")
        sites = int(sites_text) if sites_text.isdigit() else 0
        identity = "|".join((legal_name.casefold(), dba.casefold(), row.get("enrolled", "")))
        external_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        record = company_record(
            source="everify_large_employer", source_external_id=external_id,
            source_url=source_url, source_observed_at=observed_at,
            legal_name=legal_name, trade_name=dba or legal_name, country="US",
            states=row.get("states") or [], employee_size="10000+",
            metadata={
                "brand_name": dba or legal_name,
                "workforce_range": workforce,
                "employee_count_min": 10000,
                "hiring_sites": sites,
                "enrolled_at": row.get("enrolled") or None,
                "visible_states": row.get("states") or [],
                "additional_state_count": row.get("additional_states", 0),
                "employer_status": "active",
                "source_kind": "public_everify_mirror",
                "source_caveat": "requires official identity and domain verification",
            },
        )
        record["discovery_confidence"] = 0.84
        records.append(mark_employer_candidate(record))
    return records


def fetch_large_everify_employers(*, limit: int = 3000, min_interval: float = 0.75,
                                  client: httpx.Client | None = None) -> list[dict]:
    """Fetch active 10k+ workforce employers, ranked by participating hiring sites."""
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT})
    output: list[dict] = []
    seen: set[str] = set()
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for page in range(1, 66):
            response = None
            for attempt in range(6):
                response = client.get(EVERIFY_MIRROR_URL, params={
                    "status": "Open", "size": "10,000 and over",
                    "sort": "sites_desc", "page": page,
                })
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt < 5:
                    retry_after = response.headers.get("retry-after", "")
                    delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() \
                        else min(2 ** attempt, 12)
                    time.sleep(delay)
            assert response is not None
            response.raise_for_status()
            records = parse_everify_employer_page(
                response.text, source_url=str(response.url), observed_at=observed)
            if not records:
                break
            for record in records:
                name = re.sub(r"[^a-z0-9]+", " ", record["trade_name"].casefold()).strip()
                if name and name not in seen:
                    seen.add(name)
                    output.append(record)
                    if len(output) >= limit:
                        return output
            if page < 65 and min_interval:
                time.sleep(max(0.0, min_interval))
        return output
    finally:
        if owned:
            client.close()


def mandatory_employer_records() -> list[dict]:
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = []
    for brand, legal_name, domain in MANDATORY_EMPLOYERS:
        record = company_record(
            source="mandatory_employer", source_external_id=domain,
            source_url=f"https://{domain}/", source_observed_at=observed,
            legal_name=legal_name, trade_name=brand, domain=domain, country="US",
            metadata={"brand_name": brand, "mandatory_seed": True,
                      "identity_state": "requires_two_source_verification"},
        )
        record["discovery_confidence"] = 0.75
        records.append(mark_employer_candidate(record))
    return records


def _qid(value: str | None) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _binding_value(binding: dict, key: str) -> str:
    return str(((binding.get(key) or {}).get("value") or "")).strip()


def _employee_count(value: str) -> int | None:
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 1:
            return None
        return int(number)
    except (InvalidOperation, ValueError):
        return None


def parse_employer_bindings(bindings: list[dict], labels: dict[str, str], *,
                            observed_at: str) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for binding in bindings:
        qid = _qid(_binding_value(binding, "item"))
        website = _binding_value(binding, "officialWebsite")
        employees = _employee_count(_binding_value(binding, "employeeCount"))
        name = labels.get(qid, "").strip()
        host = (urlparse(website).hostname or "").lower().removeprefix("www.")
        if (not qid or not name or not host or employees is None or qid in seen
                or any(host == suffix or host.endswith("." + suffix)
                       for suffix in _BLOCKED_DOMAIN_SUFFIXES)):
            continue
        seen.add(qid)
        hq_qid = _qid(_binding_value(binding, "hq"))
        industry_qid = _qid(_binding_value(binding, "sector"))
        headquarters = labels.get(hq_qid, "").strip()
        industry = labels.get(industry_qid, "").strip()
        record = company_record(
            source="wikidata_employer", source_external_id=qid,
            source_url=f"https://www.wikidata.org/wiki/{qid}",
            source_observed_at=observed_at, legal_name=name, trade_name=name,
            domain=host, country="US", industry=industry,
            employee_size=str(employees),
            metadata={
                "wikidata_qid": qid, "brand_name": name,
                "employee_count": employees, "employee_count_property": "P1128",
                "official_website_property": "P856", "headquarters": headquarters,
                "headquarters_qid": hq_qid or None,
                "industry_qid": industry_qid or None,
                "identity_state": "structured_candidate_requires_site_verification",
            },
        )
        record["discovery_confidence"] = 0.82
        records.append(mark_employer_candidate(record))
    records.sort(key=lambda row: (-int(row["metadata"]["employee_count"]),
                                  row["legal_name"].casefold()))
    return records


def _request_json(client: httpx.Client, method: str, url: str, *, retries: int = 2,
                  **kwargs) -> dict:
    for attempt in range(retries + 1):
        try:
            response = getattr(client, method)(url, **kwargs)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            return response.json()
        if attempt < retries:
            time.sleep(0.5 * (2 ** attempt))
    status = response.status_code if response is not None else "network"
    raise RuntimeError(f"employer source unavailable: {url} ({status})")


def _labels(client: httpx.Client, ids: set[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    ordered = sorted(qid for qid in ids if qid.startswith("Q"))
    for start in range(0, len(ordered), 50):
        payload = _request_json(client, "get", WIKIDATA_API, params={
            "action": "wbgetentities", "ids": "|".join(ordered[start:start + 50]),
            "props": "labels", "languages": "en", "format": "json", "origin": "*",
        })
        for qid, entity in (payload.get("entities") or {}).items():
            label = (((entity.get("labels") or {}).get("en") or {}).get("value") or "")
            if label:
                output[qid] = label
    return output


def fetch_wikidata_employers(*, limit: int = 2500, min_employees: int = 500,
                             client: httpx.Client | None = None) -> list[dict]:
    """Fetch US organisations ranked by a published employee count."""
    limit = max(1, min(int(limit), 5000))
    min_employees = max(1, int(min_employees))
    query = f"""
      SELECT ?item (MAX(?employees) AS ?employeeCount)
             (SAMPLE(?website) AS ?officialWebsite)
             (SAMPLE(?headquarters) AS ?hq) (SAMPLE(?industry) AS ?sector)
      WHERE {{
        ?item <http://www.wikidata.org/prop/direct/P17>
              <http://www.wikidata.org/entity/Q30> ;
              <http://www.wikidata.org/prop/direct/P1128> ?employees ;
              <http://www.wikidata.org/prop/direct/P856> ?website .
        OPTIONAL {{ ?item <http://www.wikidata.org/prop/direct/P159> ?headquarters }}
        OPTIONAL {{ ?item <http://www.wikidata.org/prop/direct/P452> ?industry }}
        FILTER(?employees >= {min_employees})
        FILTER NOT EXISTS {{ ?item <http://www.wikidata.org/prop/direct/P576> ?dissolved }}
      }} GROUP BY ?item ORDER BY DESC(?employeeCount) LIMIT {limit}
    """
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(90.0), headers={"User-Agent": USER_AGENT})
    try:
        payload = _request_json(
            client, "post", WIKIDATA_SPARQL,
            data={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"})
        bindings = ((payload.get("results") or {}).get("bindings") or [])
        ids = {_qid(_binding_value(row, key)) for row in bindings
               for key in ("item", "hq", "sector")}
        labels = _labels(client, ids)
        observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return parse_employer_bindings(bindings, labels, observed_at=observed)
    finally:
        if owned:
            client.close()


def fetch_gleif_employer_candidates(*, limit: int = 15000,
                                    client: httpx.Client | None = None) -> list[dict]:
    """Fetch a diverse US legal-identity reservoir beyond GLEIF's 10k offset cap.

    Pages are round-robined across legal jurisdictions.  Each partition stays below
    the API's 10,000-result offset ceiling and LEIs are deduplicated globally.
    """
    if limit < 1:
        return []
    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT})
    jurisdictions = [f"US-{state}" for state in sorted(US_STATE_CODES)]
    output: list[dict] = []
    seen: set[str] = set()
    exhausted: set[str] = set()
    page = 1
    try:
        while len(output) < limit and len(exhausted) < len(jurisdictions) and page <= 50:
            progressed = False
            for jurisdiction in jurisdictions:
                if jurisdiction in exhausted:
                    continue
                body = _request_json(client, "get", GLEIF_LEI_RECORDS_URL, params={
                    "filter[entity.legalAddress.country]": "US",
                    "filter[entity.jurisdiction]": jurisdiction,
                    "filter[entity.category]": "GENERAL",
                    "filter[entity.status]": "ACTIVE",
                    "page[number]": page,
                    "page[size]": 200,
                })
                parsed = parse_gleif_lei_records(body)
                if not parsed or not ((body.get("links") or {}).get("next")):
                    exhausted.add(jurisdiction)
                for record in parsed:
                    lei = str(record["source_external_id"])
                    if lei in seen:
                        continue
                    seen.add(lei)
                    output.append(mark_employer_candidate(record))
                    progressed = True
                    if len(output) >= limit:
                        return output
            if not progressed:
                break
            page += 1
        return output
    finally:
        if owned:
            client.close()


def fetch_employer_reservoir(*, reservoir_min: int = 15000,
                             everify_limit: int = 3000,
                             wikidata_limit: int = 5000,
                             usaspending_limit: int = 2000,
                             gleif_limit: int = 15000,
                             min_employees: int = 500) -> list[dict]:
    """Build a scalable, source-backed reservoir for deterministic selection.

    GLEIF contributes authoritative active US legal identities, explicitly marked as
    employer candidates rather than proven employers.  E-Verify/Wikidata contribute
    employer signals with their original caveats.  No web/domain enrichment occurs.
    """
    if reservoir_min < 1:
        raise ValueError("reservoir_min must be positive")
    mandatory = [mark_employer_candidate(row) for row in mandatory_employer_records()]
    everify = fetch_large_everify_employers(limit=max(1, int(everify_limit)))
    wikidata = fetch_wikidata_employers(
        limit=max(1, int(wikidata_limit)), min_employees=min_employees)
    usaspending = [mark_employer_candidate(row) for row in fetch_usaspending_recipients(
        limit=max(1, int(usaspending_limit)), max_pages=20)]
    gleif = fetch_gleif_employer_candidates(limit=max(1, int(gleif_limit)))
    reservoir = mandatory + everify + wikidata + usaspending + gleif
    if len(reservoir) < reservoir_min:
        raise RuntimeError(
            f"employer reservoir produced only {len(reservoir)} candidates; "
            f"need at least {reservoir_min}")
    return reservoir
