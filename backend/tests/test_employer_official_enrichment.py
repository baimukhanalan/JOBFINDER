import json
from contextlib import contextmanager

import httpx
import pytest

from backend.tools.employer_official_enrichment import (
    FDIC_INSTITUTIONS,
    SAM_ENTITIES,
    apply_linked_fdic_enrichment,
    fetch_fdic_enrichment,
    fetch_sec_enrichment,
    fetch_sam_enrichment,
    parse_fdic_enrichment,
    parse_irs_990_index,
    parse_irs_990_xml,
    parse_irs_bmf_csv,
    parse_sam_enrichment,
    parse_sec_enrichment,
)


OBSERVED = "2026-08-26T12:00:00+00:00"


def test_sec_facts_are_cik_bound_and_keep_source_date_and_address_type():
    result = parse_sec_enrichment({
        "cik": 1018724, "sic": "5961", "sicDescription": "Retail-Catalog",
        "addresses": {"business": {"street1": "410 Terry Ave N", "city": "Seattle",
                                     "stateOrCountry": "WA", "zipCode": "98109"}},
    }, {
        "cik": 1018724, "facts": {"dei": {"EntityNumberOfEmployees": {
            "units": {"employees": [
                {"val": 1500000, "form": "10-K", "end": "2024-12-31",
                 "filed": "2025-02-01", "accn": "old"},
                {"val": 1576000, "form": "10-K", "end": "2025-12-31",
                 "filed": "2026-02-01", "accn": "new"},
                {"val": 9999999, "form": "8-K", "end": "2026-01-01",
                 "filed": "2026-02-02", "accn": "ignore"},
            ]}}}},
    }, observed_at=OBSERVED)
    assert result["entity_id"] == "sec_cik:0001018724"
    employees = next(f for f in result["facts"] if f["field"] == "employee_count")
    assert employees["value"] == 1576000
    assert employees["provenance"]["source_date"] == "2026-02-01"
    assert employees["qualifiers"]["as_of"] == "2025-12-31"
    address = next(f for f in result["facts"] if f["field"] == "headquarters_address")
    assert address["address_type"] == "registrant_business_address"
    assert address["qualifiers"]["operational_hq_confirmed"] is False
    assert "naics" not in result["coverage"]


def test_sec_rejects_mismatched_entity_ids():
    with pytest.raises(ValueError, match="same CIK"):
        parse_sec_enrichment({"cik": 1}, {"cik": 2})


def test_live_sec_connector_requires_a_compliant_caller_identity(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        fetch_sec_enrichment("1018724")


def test_fdic_requires_exact_cert_and_does_not_invent_headcount_or_naics():
    payload = {"meta": {"index": {"createTimestamp": "2026-08-25T00:00:00Z"}},
               "data": [{"data": {"CERT": "628", "NAME": "JPMorgan Chase Bank",
                                    "ADDRESS": "1111 Polaris Pkwy", "CITY": "Columbus",
                                    "STALP": "OH", "ZIP": "43240", "BKCLASS": "N",
                                    "SPECGRP": "4", "SPECGRPN": "Commercial Lending"}}]}
    result = parse_fdic_enrichment(payload, "628", observed_at=OBSERVED)
    assert result["entity_id"] == "fdic_cert:628"
    assert result["coverage"] == ["headquarters_address", "industry_classification"]
    assert result["facts"][0]["provenance"]["source_date"] == "2026-08-25T00:00:00Z"
    assert all(f["field"] not in {"employee_count", "naics"} for f in result["facts"])


def test_fdic_website_is_only_separate_unverified_proposed_evidence():
    payload = {"data": [{"data": {
        "CERT": "628", "NAME": "Bank", "WEBADDR": "https://www.chase.com/",
        "ADDRESS": "1111 Polaris Pkwy", "CITY": "Columbus", "STALP": "OH",
        "BKCLASS": "N"}}]}
    result = parse_fdic_enrichment(payload, "628", observed_at=OBSERVED)
    proposal = result["proposed_domain_evidence"]
    assert proposal["status"] == "proposed"
    assert proposal["verified"] is False
    assert proposal["domain"] == "chase.com"
    assert proposal["entity_id"] == "fdic_cert:628"
    assert proposal["requires"] == "independent_live_official_site_identity"


def test_fdic_nonofficial_ats_website_is_not_proposed():
    payload = {"data": [{"data": {
        "CERT": "628", "NAME": "Bank", "WEBADDR": "https://bank.icims.com/jobs",
        "ADDRESS": "1 Main", "CITY": "Columbus", "STALP": "OH", "BKCLASS": "N"}}]}
    result = parse_fdic_enrichment(payload, "628", observed_at=OBSERVED)
    assert result["proposed_domain_evidence"] is None


def test_irs_bmf_is_ein_filtered_and_labels_mailing_address():
    csv_text = ("EIN,NAME,STREET,CITY,STATE,ZIP,NTEE_CD\n"
                "123456789,Wanted Org,1 Main St,Boston,MA,02110,E20\n"
                "987654321,Other Org,2 Main St,Miami,FL,33101,B30\n")
    rows = parse_irs_bmf_csv(csv_text, {"12-3456789"}, observed_at=OBSERVED,
                             source_url="https://www.irs.gov/pub/irs-soi/eo1.csv")
    assert [row["entity_id"] for row in rows] == ["irs_ein:123456789"]
    assert rows[0]["facts"][0]["value"] == {"system": "IRS_NTEE", "code": "E20"}
    assert rows[0]["facts"][1]["address_type"] == "irs_exempt_org_mailing_address"


def test_irs_index_only_emits_requested_ein_and_official_xml_urls():
    payload = {"Filings990": [
        {"EIN": "123456789", "URL": "https://apps.irs.gov/pub/epostcard/990/xml/2025/a.xml",
         "TaxPeriod": "202412", "ReturnType": "990", "ObjectId": "a"},
        {"EIN": "123456789", "URL": "https://evil.example/a.xml"},
        {"EIN": "987654321", "URL": "https://apps.irs.gov/pub/epostcard/990/xml/2025/b.xml"},
    ]}
    refs = parse_irs_990_index(payload, {"123456789"}, source_url="index.json")
    assert len(refs) == 1
    assert refs[0]["entity_id"] == "irs_ein:123456789"
    assert refs[0]["object_id"] == "a"


def test_irs_xml_binds_employee_count_to_expected_ein_and_tax_year():
    xml = b'''<Return xmlns="http://www.irs.gov/efile">
      <ReturnHeader><Filer><EIN>123456789</EIN><USAddress>
        <AddressLine1Txt>1 Main St</AddressLine1Txt><CityNm>Boston</CityNm>
        <StateAbbreviationCd>MA</StateAbbreviationCd><ZIPCd>02110</ZIPCd>
      </USAddress></Filer><TaxYr>2024</TaxYr></ReturnHeader>
      <ReturnData><IRS990><TotalEmployeeCnt>321</TotalEmployeeCnt>
        <ActivityOrMissionDesc>Community health services</ActivityOrMissionDesc>
      </IRS990></ReturnData></Return>'''
    result = parse_irs_990_xml(xml, "12-3456789",
                               source_url="https://apps.irs.gov/pub/epostcard/990/xml/2025/a.xml",
                               observed_at=OBSERVED)
    employees = next(f for f in result["facts"] if f["field"] == "employee_count")
    assert employees["value"] == 321
    assert employees["qualifiers"]["tax_year"] == "2024"
    address = next(f for f in result["facts"] if f["field"] == "headquarters_address")
    assert address["address_type"] == "irs_filing_mailing_address"
    with pytest.raises(ValueError, match="does not match"):
        parse_irs_990_xml(xml, "987654321", source_url="official.xml")


def _sam_payload():
    return {"entityData": [{
        "entityRegistration": {"ueiSAM": "ABCDEF123456", "lastUpdateDate": "2026-08-20"},
        "coreData": {"physicalAddress": {"addressLine1": "1 Federal Way",
                                          "city": "Arlington", "stateOrProvinceCode": "VA"}},
        "assertions": {"goodsAndServices": {"naicsList": [
            {"naicsCode": "541511", "naicsDescription": "Custom Computer Programming",
             "sbaSmallBusiness": "Y"}, {"naicsCode": "not-a-code"},
        ]}},
    }]}


def test_sam_emits_naics_and_registration_address_for_exact_uei():
    result = parse_sam_enrichment(_sam_payload(), "ABCDEF123456", observed_at=OBSERVED)
    assert result["entity_id"] == "sam_uei:ABCDEF123456"
    naics = next(f for f in result["facts"] if f["field"] == "naics")
    assert naics["value"][0]["code"] == "541511"
    assert len(naics["value"]) == 1
    address = next(f for f in result["facts"] if f["field"] == "headquarters_address")
    assert address["address_type"] == "sam_registration_physical_address"


def test_sam_fetch_requires_key_and_sends_secret_only_in_header():
    with pytest.raises(RuntimeError, match="SAM_API_KEY"):
        fetch_sam_enrichment("ABCDEF123456", api_key="")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=_sam_payload(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_sam_enrichment("ABCDEF123456", api_key="secret", client=client,
                                      retries=0)
    assert result["entity_id"] == "sam_uei:ABCDEF123456"
    assert requests[0].url.path.endswith("/entities")
    assert requests[0].headers["x-api-key"] == "secret"
    assert "api_key" not in requests[0].url.params
    assert "secret" not in str(requests[0].url)
    assert requests[0].url.params["includeSections"] == "entityRegistration,coreData,assertions"


def test_fdic_fetch_is_bounded_to_requested_certificate():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"data": {
            "CERT": "628", "NAME": "Bank", "CITY": "Columbus", "STALP": "OH"}}]},
            request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_fdic_enrichment("628", client=client, retries=0)
    assert result["entity_id"] == "fdic_cert:628"
    assert requests[0].url.host == "api.fdic.gov"
    assert requests[0].url.params["filters"] == "CERT:628"
    assert requests[0].url.params["limit"] == "2"


def test_linked_fdic_apply_saves_typed_facts_and_never_domain_flags(monkeypatch):
    enrichment = parse_fdic_enrichment({"meta": {"index": {
        "createTimestamp": "2026-08-25T17:17:35Z"}}, "data": [{"data": {
            "CERT": "628", "NAME": "Bank", "ADDRESS": "1111 Polaris Pkwy",
            "CITY": "Columbus", "STALP": "OH", "ZIP": "43240",
            "BKCLASS": "N", "SPECGRPN": "International Specialization"}}]},
        "628", observed_at=OBSERVED)

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))

        def fetchall(self):
            return [(7, "628", None, None, False)]

    cursor = Cursor()

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr("backend.tools.employer_official_enrichment.company_db._cur",
                        fake_cur)
    result = apply_linked_fdic_enrichment([
        {"company_id": 7, "fdic_cert": "628", "enrichment": enrichment}])
    assert result == {"selected": 1, "updated": 1, "industry_filled": 1,
                      "headquarters_filled": 1}
    sql = " ".join(call[0] for call in cursor.calls)
    assert "domain_verified" not in sql.lower().replace("m.domain_verified", "")
    master_params = cursor.calls[1][1]
    assert master_params[1] == "1111 Polaris Pkwy, Columbus, OH, 43240"
    assert '"address_type": "fdic_institution_main_office"' in master_params[2]
    assert '"gaps": ["employee_count", "naics"]' in master_params[2]


def test_every_emitted_fact_repeats_entity_id():
    result = parse_sam_enrichment(_sam_payload(), "ABCDEF123456", observed_at=OBSERVED)
    assert result["facts"]
    assert {fact["entity_id"] for fact in result["facts"]} == {result["entity_id"]}
