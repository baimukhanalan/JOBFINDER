import httpx

from backend.tools.employer_sources import (
    MANDATORY_EMPLOYERS, fetch_employer_reservoir,
    fetch_gleif_employer_candidates, mandatory_employer_records,
    mark_employer_candidate, parse_employer_bindings, parse_everify_employer_page,
)


def test_mandatory_seed_contains_original_mass_hiring_list():
    records = mandatory_employer_records()
    assert len(records) == 15 == len(MANDATORY_EMPLOYERS)
    assert {row["trade_name"] for row in records} == {
        "Amazon", "Concentrix", "Foundever", "TTEC", "Teleperformance",
        "CVS Health", "UnitedHealth Group", "JPMorgan Chase", "Walmart",
        "Target", "Hilton", "Marriott", "Progressive", "State Farm", "Allstate",
    }
    assert all(row["metadata"]["mandatory_seed"] for row in records)
    assert all(row["metadata"]["identity_state"].startswith("requires_") for row in records)
    assert all(row["metadata"]["source_class"] == "curated_candidate" for row in records)


def test_structured_employer_requires_name_domain_and_employee_count():
    bindings = [{
        "item": {"value": "http://www.wikidata.org/entity/Q1"},
        "employeeCount": {"value": "250000"},
        "officialWebsite": {"value": "https://www.example.com/about"},
        "hq": {"value": "http://www.wikidata.org/entity/Q60"},
        "sector": {"value": "http://www.wikidata.org/entity/Q4830453"},
    }, {
        "item": {"value": "http://www.wikidata.org/entity/Q2"},
        "employeeCount": {"value": "not-known"},
        "officialWebsite": {"value": "https://invalid.example"},
    }]
    records = parse_employer_bindings(
        bindings, {"Q1": "Example Employer", "Q60": "New York City",
                   "Q4830453": "business"}, observed_at="2026-08-26T00:00:00Z")
    assert len(records) == 1
    row = records[0]
    assert row["source_external_id"] == "Q1"
    assert row["domain"] == "example.com"
    assert row["employee_size"] == "250000"
    assert row["industry"] == "business"
    assert row["metadata"]["headquarters"] == "New York City"
    assert row["metadata"]["official_website_property"] == "P856"
    assert row["metadata"]["source_class"] == "candidate_structured"


def test_structured_employer_rejects_social_profile_as_official_domain():
    binding = [{
        "item": {"value": "http://www.wikidata.org/entity/Q9"},
        "employeeCount": {"value": "5000"},
        "officialWebsite": {"value": "https://x.com/example"},
    }]
    assert parse_employer_bindings(
        binding, {"Q9": "Example"}, observed_at="2026-08-26T00:00:00Z") == []


def test_everify_large_employer_parser_preserves_workforce_and_hiring_sites():
    html = '''<table><tr class="evm-tr">
      <td><div class="evm-enm">Example Holdings, Inc.</div>
      <div class="evm-dba">DBA: Example Brand</div></td>
      <td class="evm-tdsz">10,000 and over</td>
      <td><span class="evm-stag">CA</span><span class="evm-stag">TX</span>
      <span class="evm-stag evm-stag-more">+3</span></td>
      <td class="evm-tddt">Jan 2, 2020</td><td><span class="evm-sites">1,234</span></td>
    </tr></table>'''
    records = parse_everify_employer_page(
        html, source_url="https://source.test/page", observed_at="2026-08-26T00:00:00Z")
    assert len(records) == 1
    row = records[0]
    assert row["legal_name"] == "Example Holdings, Inc."
    assert row["trade_name"] == "Example Brand"
    assert row["employee_size"] == "10000+"
    assert row["states"] == ["CA", "TX"]
    assert row["metadata"]["employee_count_min"] == 10000
    assert row["metadata"]["hiring_sites"] == 1234
    assert row["metadata"]["additional_state_count"] == 3
    assert row["metadata"]["employer_evidence"] == "workforce_range_10000_plus"


def test_candidate_marker_keeps_segments_and_risks_explicit():
    row = mark_employer_candidate({
        "source": "gleif_lei", "legal_name": "Acme Staffing Fund LLC",
        "trade_name": "Acme Staffing Fund LLC", "metadata": {},
    })
    assert row["metadata"]["source_class"] == "authoritative_registry"
    assert row["metadata"]["employer_evidence"] == "legal_identity_only"
    assert row["metadata"]["employer_segment"] == "staffing"
    assert "fund_or_trust" in row["metadata"]["risk_flags"]


def test_reservoir_combines_real_source_records_without_enrichment(monkeypatch):
    from backend.tools import employer_sources as sources

    def rows(source, count):
        return [mark_employer_candidate({
            "source": source, "source_external_id": f"{source}-{i}",
            "legal_name": f"Employer {source} {i}",
            "trade_name": f"Employer {source} {i}", "country": "US", "metadata": {},
        }) for i in range(count)]

    monkeypatch.setattr(sources, "mandatory_employer_records",
                        lambda: rows("mandatory_employer", 15))
    monkeypatch.setattr(sources, "fetch_large_everify_employers",
                        lambda **_: rows("everify_large_employer", 20))
    monkeypatch.setattr(sources, "fetch_wikidata_employers",
                        lambda **_: rows("wikidata_employer", 25))
    monkeypatch.setattr(sources, "fetch_usaspending_recipients",
                        lambda **_: rows("usaspending", 10))
    monkeypatch.setattr(sources, "fetch_gleif_employer_candidates",
                        lambda **_: rows("gleif_lei", 30))
    reservoir = fetch_employer_reservoir(
        reservoir_min=100, everify_limit=20, wikidata_limit=25,
        usaspending_limit=10, gleif_limit=30)
    assert len(reservoir) == 100
    assert all(row["metadata"]["employer_candidate"] for row in reservoir)
    assert not any(row.get("careers_url") for row in reservoir)


def test_gleif_reservoir_partitions_by_us_jurisdiction():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, request=request, json={
            "meta": {"goldenCopy": {"publishDate": "2026-08-26T00:00:00Z"}},
            "links": {"next": "page2"},
            "data": [{
                "id": "LEI1", "attributes": {"lei": "LEI1", "entity": {
                    "legalName": {"name": "Example Corporation"},
                    "legalAddress": {"country": "US", "region": "US-AK"},
                    "jurisdiction": "US-AK", "category": "GENERAL", "status": "ACTIVE",
                }},
            }],
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_gleif_employer_candidates(limit=1, client=client)
    assert len(rows) == 1
    assert seen[0]["filter[entity.jurisdiction]"].startswith("US-")
    assert rows[0]["metadata"]["source_class"] == "authoritative_registry"
