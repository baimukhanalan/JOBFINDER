import httpx
import io
import json
import zipfile

from backend.tools import company_sources as cs


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_record_contract_and_domain_normalization():
    row = cs.company_record(
        source="x", source_external_id=7, legal_name=" Acme Inc ",
        domain="https://www.Acme.com/about", states=["ca", "CA", "ny"],
    )
    assert tuple(row) == cs.RECORD_FIELDS
    assert row["domain"] == "acme.com"
    assert row["states"] == ["CA", "NY"]
    assert row["metadata"] == {}


def test_parse_sec_tickers_is_bounded_and_normalized():
    rows = cs.parse_sec_tickers({
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 320193, "ticker": "APC.F", "title": "Apple Inc."},
        "2": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }, limit=2)
    assert len(rows) == 2
    assert rows[0]["source"] == "sec_edgar"
    assert rows[0]["source_external_id"] == "0000320193"
    assert rows[0]["metadata"]["ticker"] == "AAPL"
    assert rows[1]["source_external_id"] == "0000789019"


def test_parse_sec_submission_extracts_registry_fields():
    row = cs.parse_sec_submission({
        "cik": "320193", "name": "Apple Inc.", "sic": "3571",
        "sicDescription": "Electronic Computers", "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"], "stateOfIncorporation": "CA",
        "website": "https://www.apple.com", "entityType": "operating",
        "addresses": {
            "business": {"stateOrCountry": "CA"},
            "mailing": {"stateOrCountry": "CA"},
        },
    })
    assert row is not None
    assert row["states"] == ["CA"]
    assert row["industry"] == "Electronic Computers"
    assert row["metadata"]["sic"] == "3571"
    assert row["domain"] == "apple.com"
    assert row["metadata"]["entity_type"] == "operating"


def test_parse_sec_bulk_zip_filters_non_operating_entities():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("CIK0001.json", json.dumps({
            "cik": "1", "name": "Acme Inc", "entityType": "operating",
            "website": "https://acme.test", "addresses": {},
        }))
        archive.writestr("CIK0002.json", json.dumps({
            "cik": "2", "name": "Acme Fund", "entityType": "investment company",
            "addresses": {},
        }))
    rows = cs.parse_sec_submissions_zip(stream.getvalue())
    assert [row["legal_name"] for row in rows] == ["Acme Inc"]
    assert rows[0]["domain"] == "acme.test"


def test_fetch_sec_uses_identified_user_agent_and_can_enrich():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["user-agent"] == "JobFinder test test@example.com"
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={
                "0": {"cik_str": 1, "ticker": "ACME", "title": "Acme"},
            })
        return httpx.Response(200, json={
            "cik": "1", "name": "Acme", "sicDescription": "Services",
            "addresses": {"business": {"stateOrCountry": "NY"}},
        })

    with _client(handler) as client:
        rows = cs.fetch_sec_companies(
            limit=1, enrich_submissions=True, client=client,
            user_agent="JobFinder test test@example.com",
        )
    assert len(seen) == 2
    assert rows[0]["industry"] == "Services"
    assert rows[0]["states"] == ["NY"]


def test_usaspending_parser_and_pagination_deduplicate():
    pages = []

    def handler(request):
        body = __import__("json").loads(request.content)
        pages.append(body["page"])
        if body["page"] == 1:
            return httpx.Response(200, json={
                "results": [
                    {"name": "Acme LLC", "code": "hash-1", "amount": 20},
                    {"name": "Beta Inc", "uei": "UEI2", "amount": 10},
                ],
                "page_metadata": {"hasNext": True},
            })
        return httpx.Response(200, json={
            "results": [
                {"name": "Acme LLC", "code": "hash-1"},
                {"name": "Gamma Corp", "recipient_id": "r3", "state_code": "tx"},
            ],
            "page_metadata": {"hasNext": False},
        })

    with _client(handler) as client:
        rows = cs.fetch_usaspending_recipients(
            limit=10, page_size=2, max_pages=3,
            client=client,
        )
    assert pages == [1, 2]
    assert [row["source_external_id"] for row in rows] == ["hash-1", "UEI2", "r3"]
    assert rows[-1]["states"] == ["TX"]


def test_sam_is_optional_without_key(monkeypatch):
    monkeypatch.delenv("SAM_API_KEY", raising=False)
    assert cs.fetch_sam_companies() == []


def test_sam_parser_and_fetch_pagination():
    requested_pages = []

    def handler(request):
        requested_pages.append(int(request.url.params["page"]))
        assert request.url.params["api_key"] == "secret-test-key"
        return httpx.Response(200, json={
            "entityData": [{
                "entityRegistration": {
                    "ueiSAM": "ABC123", "legalBusinessName": "Acme Federal LLC",
                    "dbaName": "Acme", "cageCode": "1A2B3", "registrationStatus": "A",
                },
                "coreData": {"physicalAddress": {
                    "countryCode": "USA", "stateOrProvinceCode": "VA",
                }},
                "assertions": {"goodsAndServices": {
                    "naicsList": [{"naicsCode": "541511", "isPrimary": True}],
                }},
            }],
            "totalPages": 1,
        })

    with _client(handler) as client:
        rows = cs.fetch_sam_companies(
            api_key="secret-test-key", limit=10, max_pages=5, client=client,
        )
    assert requested_pages == [0]
    assert rows[0]["source"] == "sam_gov"
    assert rows[0]["source_external_id"] == "ABC123"
    assert rows[0]["trade_name"] == "Acme"
    assert rows[0]["naics"] == "541511"
    assert rows[0]["states"] == ["VA"]
