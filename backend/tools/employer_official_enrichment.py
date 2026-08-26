"""Entity-ID-bound official facts from SEC, FDIC, IRS and SAM.

This module deliberately has no database/name matching code. Callers must already
possess the provider identifier and may only attach the returned facts to that ID.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import httpx

from backend.tools import company_discovery_db as company_db
from backend.tools import employer_authoritative_sources as identity_sources


SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FDIC_INSTITUTIONS = "https://api.fdic.gov/banks/institutions"
IRS_990_INDEX = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.json"
SAM_ENTITIES = "https://api.sam.gov/entity-information/v4/entities"
USER_AGENT = "JobFinder-official-enrichment/1.0 (+https://github.com/baimukhanalan/JOBFINDER)"
TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=30.0, write=10.0, pool=10.0)
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _cik(value: Any) -> str:
    raw = _text(value)
    return raw.zfill(10) if raw.isdigit() and 1 <= len(raw) <= 10 else ""


def _ein(value: Any) -> str:
    raw = re.sub(r"\D", "", _text(value))
    return raw if len(raw) == 9 else ""


def _uei(value: Any) -> str:
    raw = _text(value).upper()
    return raw if re.fullmatch(r"[A-Z0-9]{12}", raw) else ""


def _fact(*, entity_id: str, field: str, value: Any, source_url: str,
          observed_at: str, source_date: str = "", source_field: str = "",
          unit: str = "", address_type: str = "", qualifiers: Mapping[str, Any] | None = None) -> dict:
    if not entity_id or not field or value in (None, "", [], {}):
        raise ValueError("entity_id, field and value are required")
    result = {"entity_id": entity_id, "field": field, "value": value,
              "provenance": {"source_url": source_url, "observed_at": observed_at,
                             "source_date": source_date or None,
                             "source_field": source_field or None}}
    if unit:
        result["unit"] = unit
    if address_type:
        result["address_type"] = address_type
    if qualifiers:
        result["qualifiers"] = dict(qualifiers)
    return result


def _node(provider: str, entity_id: str, facts: list[dict], *, source_url: str,
          observed_at: str, limitations: list[str] | None = None) -> dict:
    if not entity_id or any(fact.get("entity_id") != entity_id for fact in facts):
        raise ValueError("every fact must be bound to the node entity_id")
    return {"provider": provider, "entity_id": entity_id, "facts": facts,
            "coverage": sorted({fact["field"] for fact in facts}),
            "limitations": limitations or [],
            "provenance": {"source_url": source_url, "observed_at": observed_at}}


def _latest_annual_employee_fact(companyfacts: Mapping[str, Any]) -> Mapping[str, Any] | None:
    concept = _mapping(_mapping(companyfacts.get("facts")).get("dei"))
    concept = _mapping(concept.get("EntityNumberOfEmployees"))
    units = _mapping(concept.get("units"))
    rows = units.get("employees") or units.get("Employee") or []
    candidates = [row for row in rows if isinstance(row, Mapping)
                  and _text(row.get("form")).upper() in {"10-K", "10-K/A", "20-F", "40-F"}
                  and isinstance(row.get("val"), (int, float)) and row["val"] >= 0]
    return max(candidates, key=lambda row: (
        _text(row.get("end")), _text(row.get("filed")), _text(row.get("accn")))) \
        if candidates else None


def parse_sec_enrichment(submission: Any, companyfacts: Any, *,
                         observed_at: str = "", submissions_url: str = "",
                         companyfacts_url: str = "") -> dict:
    submission = _mapping(submission)
    companyfacts = _mapping(companyfacts)
    cik = _cik(submission.get("cik"))
    facts_cik = _cik(companyfacts.get("cik"))
    if not cik or facts_cik != cik:
        raise ValueError("SEC submissions and companyfacts must carry the same CIK")
    entity_id = f"sec_cik:{cik}"
    observed = observed_at or _now()
    submissions_url = submissions_url or SEC_SUBMISSIONS.format(cik=cik)
    companyfacts_url = companyfacts_url or SEC_COMPANYFACTS.format(cik=cik)
    facts: list[dict] = []
    employee = _latest_annual_employee_fact(companyfacts)
    if employee:
        facts.append(_fact(
            entity_id=entity_id, field="employee_count", value=int(employee["val"]),
            unit="employees", source_url=companyfacts_url, observed_at=observed,
            source_date=_text(employee.get("filed")),
            source_field="dei:EntityNumberOfEmployees",
            qualifiers={"as_of": _text(employee.get("end")),
                        "form": _text(employee.get("form")),
                        "accession": _text(employee.get("accn"))}))
    sic = _text(submission.get("sic"))
    sic_desc = _text(submission.get("sicDescription"))
    if sic or sic_desc:
        facts.append(_fact(
            entity_id=entity_id, field="industry_classification",
            value={"system": "SEC_SIC", "code": sic or None,
                   "description": sic_desc or None}, source_url=submissions_url,
            observed_at=observed, source_field="sic,sicDescription"))
    business = _mapping(_mapping(submission.get("addresses")).get("business"))
    address = {key: _text(business.get(key)) for key in
               ("street1", "street2", "city", "stateOrCountry", "zipCode")
               if _text(business.get(key))}
    if address:
        facts.append(_fact(
            entity_id=entity_id, field="headquarters_address", value=address,
            address_type="registrant_business_address", source_url=submissions_url,
            observed_at=observed, source_field="addresses.business",
            qualifiers={"operational_hq_confirmed": False}))
    return _node("sec_edgar", entity_id, facts, source_url=submissions_url,
                 observed_at=observed, limitations=[
                     "SEC supplies SIC, not NAICS; no SIC-to-NAICS mapping was inferred",
                     "registrant business address is not asserted as operational headquarters",
                 ])


def parse_fdic_enrichment(payload: Any, cert: Any, *, observed_at: str = "",
                          source_url: str = FDIC_INSTITUTIONS) -> dict:
    normalized = _text(cert)
    if not normalized.isdigit():
        raise ValueError("FDIC certificate must be numeric")
    wrappers = _mapping(payload).get("data") or []
    matches = []
    for wrapper in wrappers:
        row = _mapping(_mapping(wrapper).get("data") or wrapper)
        if _text(row.get("CERT")) == normalized:
            matches.append(row)
    if len(matches) != 1:
        raise ValueError("FDIC response must contain exactly the requested certificate")
    row = matches[0]
    entity_id = f"fdic_cert:{normalized}"
    observed = observed_at or _now()
    dataset_date = _text(_mapping(_mapping(_mapping(payload).get("meta")).get("index")).get(
        "createTimestamp"))
    facts: list[dict] = []
    classification = {key: _text(row.get(key)) for key in ("BKCLASS", "SPECGRP", "SPECGRPN")
                      if _text(row.get(key))}
    if classification:
        facts.append(_fact(entity_id=entity_id, field="industry_classification",
                           value={"system": "FDIC_BANK_CLASS", **classification},
                           source_url=source_url, observed_at=observed,
                           source_date=dataset_date, source_field="BKCLASS,SPECGRP,SPECGRPN"))
    address = {key: _text(row.get(key)) for key in ("ADDRESS", "CITY", "STALP", "ZIP", "COUNTY")
               if _text(row.get(key))}
    if address:
        facts.append(_fact(entity_id=entity_id, field="headquarters_address", value=address,
                           address_type="fdic_institution_main_office",
                           source_url=source_url, observed_at=observed,
                           source_date=dataset_date,
                           source_field="ADDRESS,CITY,STALP,ZIP,COUNTY"))
    result = _node("fdic_bankfind", entity_id, facts, source_url=source_url,
                   observed_at=observed, limitations=[
                       "BankFind institution records do not provide consolidated employer headcount",
                       "FDIC bank class is not NAICS and was not converted",
                   ])
    assertion = identity_sources.domain_assertion(
        provider="fdic_bankfind", entity_id=entity_id, value=row.get("WEBADDR"),
        source_field="WEBADDR", assertion_type="institution_reported_primary_website",
        source_url=source_url, observed_at=observed)
    result["proposed_domain_evidence"] = ({
        "status": "proposed", "class": "official_regulator_domain_assertion",
        "assertion": "institution_reported_primary_website",
        "verified": False, "requires": "independent_live_official_site_identity",
        **assertion,
    } if assertion else None)
    return result


def parse_irs_bmf_csv(content: bytes | str, requested_eins: set[str], *,
                      observed_at: str = "", source_url: str = "") -> list[dict]:
    requested = {_ein(value) for value in requested_eins} - {""}
    if not requested:
        return []
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    observed = observed_at or _now()
    output = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {str(key).upper(): value for key, value in raw.items()}
        ein = _ein(row.get("EIN"))
        if ein not in requested:
            continue
        entity_id = f"irs_ein:{ein}"
        facts = []
        ntee = _text(row.get("NTEE_CD"))
        if ntee:
            facts.append(_fact(entity_id=entity_id, field="industry_classification",
                               value={"system": "IRS_NTEE", "code": ntee},
                               source_url=source_url, observed_at=observed,
                               source_field="NTEE_CD"))
        address = {key.lower(): _text(row.get(key)) for key in
                   ("STREET", "CITY", "STATE", "ZIP") if _text(row.get(key))}
        if address:
            facts.append(_fact(entity_id=entity_id, field="headquarters_address", value=address,
                               address_type="irs_exempt_org_mailing_address",
                               source_url=source_url, observed_at=observed,
                               source_field="STREET,CITY,STATE,ZIP",
                               qualifiers={"operational_hq_confirmed": False}))
        output.append(_node("irs_exempt_org_bmf", entity_id, facts, source_url=source_url,
                            observed_at=observed, limitations=[
                                "BMF contains NTEE, not NAICS",
                                "BMF mailing address is not asserted as operational headquarters",
                                "BMF does not contain employee headcount",
                            ]))
    return output


def parse_irs_990_index(payload: Any, requested_eins: set[str], *, limit: int = 100,
                        source_url: str = "", observed_at: str = "") -> list[dict]:
    requested = {_ein(value) for value in requested_eins} - {""}
    rows = _mapping(payload).get("Filings990") or _mapping(payload).get("filings") or []
    observed = observed_at or _now()
    refs = []
    for row in rows:
        row = _mapping(row)
        ein = _ein(row.get("EIN") or row.get("ein"))
        url = _text(row.get("URL") or row.get("url"))
        parsed = urlparse(url)
        if ein not in requested or parsed.scheme != "https" or parsed.hostname != "apps.irs.gov" \
                or not parsed.path.startswith("/pub/epostcard/990/xml/") \
                or not parsed.path.endswith(".xml"):
            continue
        refs.append({"provider": "irs_990_efile", "entity_id": f"irs_ein:{ein}",
                     "filing_url": url, "tax_period": _text(row.get("TaxPeriod")),
                     "return_type": _text(row.get("ReturnType")),
                     "object_id": _text(row.get("ObjectId")),
                     "provenance": {"source_url": source_url, "observed_at": observed}})
        if len(refs) >= max(0, int(limit)):
            break
    return refs


def _xml_values(root: ET.Element, local_name: str) -> list[ET.Element]:
    return [element for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == local_name]


def parse_irs_990_xml(content: bytes, expected_ein: Any, *, source_url: str,
                      observed_at: str = "") -> dict:
    expected = _ein(expected_ein)
    if not expected:
        raise ValueError("expected EIN must contain nine digits")
    if len(content) > 15_000_000:
        raise ValueError("IRS filing exceeds XML size limit")
    root = ET.fromstring(content)
    reported = next((_ein(element.text) for element in _xml_values(root, "EIN")
                     if _ein(element.text)), "")
    if reported != expected:
        raise ValueError("IRS filing EIN does not match the requested entity")
    entity_id = f"irs_ein:{expected}"
    observed = observed_at or _now()
    tax_year = next((_text(element.text) for element in _xml_values(root, "TaxYr")
                     if _text(element.text)), "")
    facts = []
    employees = next((_text(element.text) for element in _xml_values(root, "TotalEmployeeCnt")
                      if _text(element.text).isdigit()), "")
    if employees:
        facts.append(_fact(entity_id=entity_id, field="employee_count", value=int(employees),
                           unit="employees", source_url=source_url, observed_at=observed,
                           source_date=tax_year, source_field="TotalEmployeeCnt",
                           qualifiers={"tax_year": tax_year}))
    mission = next((_text(element.text) for element in _xml_values(root, "ActivityOrMissionDesc")
                    if _text(element.text)), "")
    if mission:
        facts.append(_fact(entity_id=entity_id, field="industry_description", value=mission,
                           source_url=source_url, observed_at=observed,
                           source_date=tax_year, source_field="ActivityOrMissionDesc",
                           qualifiers={"classification": False}))
    address_element = next(iter(_xml_values(root, "USAddress")), None)
    if address_element is not None:
        address = {child.tag.rsplit("}", 1)[-1]: _text(child.text)
                   for child in address_element if _text(child.text)}
        if address:
            facts.append(_fact(entity_id=entity_id, field="headquarters_address", value=address,
                               address_type="irs_filing_mailing_address",
                               source_url=source_url, observed_at=observed,
                               source_date=tax_year, source_field="USAddress",
                               qualifiers={"operational_hq_confirmed": False}))
    return _node("irs_990_efile", entity_id, facts, source_url=source_url,
                 observed_at=observed, limitations=[
                     "Form 990 mailing address is not asserted as operational headquarters",
                     "mission description is narrative and is not NAICS",
                 ])


def parse_sam_enrichment(payload: Any, expected_uei: Any, *, observed_at: str = "",
                         source_url: str = SAM_ENTITIES) -> dict:
    expected = _uei(expected_uei)
    if not expected:
        raise ValueError("expected UEI must be 12 alphanumeric characters")
    rows = _mapping(payload).get("entityData") or _mapping(payload).get("entities") or []
    matches = []
    for row in rows:
        registration = _mapping(_mapping(row).get("entityRegistration"))
        if _uei(registration.get("ueiSAM") or _mapping(row).get("ueiSAM")) == expected:
            matches.append(_mapping(row))
    if len(matches) != 1:
        raise ValueError("SAM response must contain exactly the requested UEI")
    row = matches[0]
    entity_id = f"sam_uei:{expected}"
    observed = observed_at or _now()
    registration = _mapping(row.get("entityRegistration"))
    core = _mapping(row.get("coreData"))
    assertions = _mapping(row.get("assertions"))
    source_date = _text(registration.get("lastUpdateDate") or
                        registration.get("registrationDate"))
    facts = []
    physical = _mapping(core.get("physicalAddress"))
    address = {str(key): _text(value) for key, value in physical.items() if _text(value)}
    if address:
        facts.append(_fact(entity_id=entity_id, field="headquarters_address", value=address,
                           address_type="sam_registration_physical_address",
                           source_url=source_url, observed_at=observed,
                           source_date=source_date, source_field="coreData.physicalAddress",
                           qualifiers={"operational_hq_confirmed": False}))
    goods = _mapping(assertions.get("goodsAndServices"))
    naics = []
    for item in goods.get("naicsList") or []:
        item = _mapping(item)
        code = _text(item.get("naicsCode"))
        if re.fullmatch(r"\d{2,6}", code):
            naics.append({"code": code, "description": _text(item.get("naicsDescription")) or None,
                          "sba_small_business": item.get("sbaSmallBusiness")})
    if naics:
        facts.append(_fact(entity_id=entity_id, field="naics", value=naics,
                           source_url=source_url, observed_at=observed,
                           source_date=source_date,
                           source_field="assertions.goodsAndServices.naicsList"))
    return _node("sam_gov", entity_id, facts, source_url=source_url,
                 observed_at=observed, limitations=[
                     "SAM physical address is registration data, not asserted operational headquarters",
                     "SAM Entity Management does not provide authoritative employee headcount",
                 ])


def _get(client: httpx.Client, url: str, *, retries: int = 2, max_bytes: int = 15_000_000,
         sleep: Callable[[float], None] = time.sleep, **kwargs: Any) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            response = client.get(url, **kwargs)
            if response.status_code in RETRYABLE and attempt < retries:
                sleep(min(0.5 * 2 ** attempt, 4.0))
                continue
            response.raise_for_status()
            if len(response.content) > max_bytes:
                raise ValueError("official enrichment response exceeds size limit")
            return response
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"official enrichment source rejected request with HTTP {exc.response.status_code}: {url}") from exc
        except httpx.TransportError as exc:
            last = exc
            if attempt < retries:
                sleep(min(0.5 * 2 ** attempt, 4.0))
                continue
            break
    raise RuntimeError(f"official enrichment source unavailable: {url}") from last


def _client(client: httpx.Client | None = None) -> tuple[httpx.Client, bool]:
    return (client, False) if client is not None else (
        httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"}), True)


def fetch_sec_enrichment(cik: Any, *, client: httpx.Client | None = None,
                         retries: int = 2, sec_user_agent: str | None = None) -> dict:
    normalized = _cik(cik)
    if not normalized:
        raise ValueError("CIK must contain 1 to 10 digits")
    if client is None:
        identity = _text(sec_user_agent or os.getenv("SEC_USER_AGENT"))
        if not identity:
            raise RuntimeError(
                "SEC_USER_AGENT is required and should identify the organization and contact email")
        http = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": identity,
                                                      "Accept": "application/json"})
        owned = True
    else:
        http, owned = _client(client)
    try:
        submissions_url = SEC_SUBMISSIONS.format(cik=normalized)
        companyfacts_url = SEC_COMPANYFACTS.format(cik=normalized)
        submission = _get(http, submissions_url, retries=retries, max_bytes=8_000_000).json()
        facts = _get(http, companyfacts_url, retries=retries, max_bytes=30_000_000).json()
        return parse_sec_enrichment(submission, facts, submissions_url=submissions_url,
                                    companyfacts_url=companyfacts_url)
    finally:
        if owned:
            http.close()


def fetch_fdic_enrichment(cert: Any, *, client: httpx.Client | None = None,
                          retries: int = 2) -> dict:
    normalized = _text(cert)
    if not normalized.isdigit():
        raise ValueError("FDIC certificate must be numeric")
    http, owned = _client(client)
    try:
        response = _get(http, FDIC_INSTITUTIONS, retries=retries, max_bytes=3_000_000,
                        params={"filters": f"CERT:{normalized}",
                                "fields": "CERT,NAME,WEBADDR,ADDRESS,CITY,STALP,ZIP,COUNTY,BKCLASS,SPECGRP,SPECGRPN",
                                "limit": 2, "format": "json"})
        return parse_fdic_enrichment(response.json(), normalized, source_url=str(response.url))
    finally:
        if owned:
            http.close()


def _classification_text(result: Mapping[str, Any]) -> str:
    fact = next((fact for fact in result.get("facts") or []
                 if fact.get("field") == "industry_classification"), None)
    value = fact.get("value") if fact else {}
    if not isinstance(value, Mapping):
        return ""
    parts = ["FDIC-regulated banking institution"]
    if value.get("BKCLASS"):
        parts.append(f'bank class {value["BKCLASS"]}')
    if value.get("SPECGRPN"):
        parts.append(str(value["SPECGRPN"]))
    return "; ".join(parts)


def _main_office(result: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None]:
    fact = next((fact for fact in result.get("facts") or []
                 if fact.get("field") == "headquarters_address"), None)
    if not fact or fact.get("address_type") != "fdic_institution_main_office":
        return "", None
    value = fact.get("value") or {}
    if not isinstance(value, Mapping):
        return "", None
    address = ", ".join(str(value.get(key)) for key in
                        ("ADDRESS", "CITY", "STALP", "ZIP") if value.get(key))
    return address, fact


def apply_linked_fdic_enrichment(rows: list[Mapping[str, Any]]) -> dict:
    """Persist official facts for exact existing CERT links, never domain state."""
    if not rows:
        return {"selected": 0, "updated": 0, "industry_filled": 0,
                "headquarters_filled": 0}
    by_company = {int(row["company_id"]): row for row in rows}
    if len(by_company) != len(rows):
        raise ValueError("duplicate company_id in FDIC enrichment batch")
    ids = sorted(by_company)
    with company_db._cur(False) as cur:
        cur.execute("""
          SELECT c.id,c.external_ids->>'fdic_cert',m.industry,m.headquarters,
                 m.domain_verified
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population AND c.id=ANY(%s) FOR UPDATE
        """, (ids,))
        locked = {int(row[0]): {"cert": str(row[1] or ""), "industry": row[2],
                               "headquarters": row[3], "domain_verified": row[4]}
                  for row in cur.fetchall()}
        if set(locked) != set(ids):
            raise RuntimeError("FDIC enrichment preflight could not lock every active link")
        industry_filled = headquarters_filled = updated = 0
        for company_id in ids:
            item = by_company[company_id]
            result = item["enrichment"]
            cert = str(item["fdic_cert"])
            if locked[company_id]["cert"] != cert or result.get("entity_id") != f"fdic_cert:{cert}":
                raise RuntimeError("FDIC enrichment entity does not match stored CERT")
            industry = _classification_text(result)
            headquarters, address_fact = _main_office(result)
            if not industry or not headquarters or address_fact is None:
                raise RuntimeError("FDIC enrichment lacks required classification/main-office facts")
            evidence = {
                "entity_id": result["entity_id"], "provider": "fdic_bankfind",
                "industry_classification": next(
                    fact for fact in result["facts"]
                    if fact["field"] == "industry_classification"),
                "main_office": address_fact,
                "gaps": ["employee_count", "naics"],
                "limitations": result.get("limitations") or [],
                "provenance": result.get("provenance") or {},
            }
            encoded = json.dumps({"fdic_official_enrichment": evidence})
            cur.execute("""
              UPDATE company_employer_master SET industry=%s,headquarters=%s,
                qualification_evidence=qualification_evidence || %s::jsonb,updated_at=now()
              WHERE company_id=%s AND in_target_population
            """, (industry, headquarters, encoded, company_id))
            if cur.rowcount != 1:
                continue
            updated += 1
            industry_filled += int(not locked[company_id]["industry"])
            headquarters_filled += int(not locked[company_id]["headquarters"])
            cur.execute("""
              UPDATE company_discovery SET industry=%s,
                provenance=provenance || %s::jsonb,updated_at=now() WHERE id=%s
                AND EXISTS (SELECT 1 FROM company_employer_master m
                  WHERE m.company_id=company_discovery.id AND m.in_target_population)
            """, (industry, encoded, company_id))
    return {"selected": len(rows), "updated": updated,
            "industry_filled": industry_filled,
            "headquarters_filled": headquarters_filled}


def enrich_linked_fdic(*, limit: int = 50, min_interval: float = 0.1,
                       fetcher: Callable[..., dict] = fetch_fdic_enrichment) -> dict:
    """Fetch and apply a bounded exact-CERT batch; return domain assertions separately."""
    with company_db._cur() as cur:
        cur.execute("""
          SELECT c.id,c.external_ids->>'fdic_cert' AS cert
          FROM company_discovery c JOIN company_employer_master m ON m.company_id=c.id
          WHERE m.in_target_population AND NULLIF(c.external_ids->>'fdic_cert','') IS NOT NULL
          ORDER BY c.id LIMIT %s
        """, (max(1, min(int(limit), 100)),))
        linked = [(int(row["id"]), str(row["cert"])) for row in cur.fetchall()]
    enriched = []
    proposals = []
    errors = []
    for index, (company_id, cert) in enumerate(linked):
        try:
            result = fetcher(cert)
            enriched.append({"company_id": company_id, "fdic_cert": cert,
                             "enrichment": result})
            proposal = result.get("proposed_domain_evidence")
            if proposal:
                proposals.append({"company_id": company_id, **proposal})
        except Exception as exc:
            errors.append({"company_id": company_id, "fdic_cert": cert, "error": str(exc)})
        if min_interval > 0 and index + 1 < len(linked):
            time.sleep(min_interval)
    applied = apply_linked_fdic_enrichment(enriched)
    return {"linked": len(linked), **applied, "errors": errors,
            "gaps": {"employee_count": len(enriched), "naics": len(enriched)},
            "proposed_domain_evidence": proposals,
            "domain_flags_changed": False}


def fetch_irs_990_xml(filing_url: str, expected_ein: Any, *,
                      client: httpx.Client | None = None, retries: int = 2) -> dict:
    parsed = urlparse(filing_url)
    if parsed.scheme != "https" or parsed.hostname != "apps.irs.gov" \
            or not parsed.path.startswith("/pub/epostcard/990/xml/") \
            or not parsed.path.endswith(".xml"):
        raise ValueError("filing_url must be an official IRS 990 XML URL")
    http, owned = _client(client)
    try:
        response = _get(http, filing_url, retries=retries, max_bytes=15_000_000,
                        headers={"Accept": "application/xml"})
        return parse_irs_990_xml(response.content, expected_ein, source_url=str(response.url))
    finally:
        if owned:
            http.close()


def fetch_sam_enrichment(uei: Any, *, api_key: str | None = None,
                         client: httpx.Client | None = None, retries: int = 2) -> dict:
    normalized = _uei(uei)
    if not normalized:
        raise ValueError("UEI must be 12 alphanumeric characters")
    key = _text(api_key or os.getenv("SAM_API_KEY"))
    if not key:
        raise RuntimeError("SAM_API_KEY is required for SAM official enrichment")
    http, owned = _client(client)
    try:
        response = _get(http, SAM_ENTITIES, retries=retries, max_bytes=10_000_000,
                        params={"ueiSAM": normalized,
                                "includeSections": "entityRegistration,coreData,assertions",
                                "page": 0, "size": 1}, headers={"X-Api-Key": key})
        return parse_sam_enrichment(response.json(), normalized, source_url=str(response.url))
    finally:
        if owned:
            http.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="provider", required=True)
    sec = sub.add_parser("sec"); sec.add_argument("--cik", required=True)
    fdic = sub.add_parser("fdic"); fdic.add_argument("--cert", required=True)
    irs = sub.add_parser("irs-xml"); irs.add_argument("--ein", required=True); irs.add_argument("--url", required=True)
    sam = sub.add_parser("sam"); sam.add_argument("--uei", required=True)
    linked = sub.add_parser("fdic-linked")
    linked.add_argument("--limit", type=int, default=50)
    linked.add_argument("--min-interval", type=float, default=0.1)
    args = parser.parse_args(argv)
    if args.provider == "sec": result = fetch_sec_enrichment(args.cik)
    elif args.provider == "fdic": result = fetch_fdic_enrichment(args.cert)
    elif args.provider == "irs-xml": result = fetch_irs_990_xml(args.url, args.ein)
    elif args.provider == "sam": result = fetch_sam_enrichment(args.uei)
    else: result = enrich_linked_fdic(limit=args.limit, min_interval=args.min_interval)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
