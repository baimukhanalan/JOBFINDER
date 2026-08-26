import io
import json
import zipfile

import httpx
import pytest

from backend.tools.employer_authoritative_sources import (
    FDIC_INSTITUTIONS_URL,
    SAM_ENTITIES_URL,
    domain_assertion,
    fetch_fdic_institutions,
    fetch_sam_entities,
    fetch_sec_tickers,
    parse_fdic_institutions,
    parse_sam_entity,
    parse_sec_submission,
    parse_sec_submissions_zip,
    parse_sec_tickers,
)


OBSERVED = "2026-08-26T10:00:00+00:00"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _fdic_row(cert, name, website, **extra):
    return {"data": {
        "CERT": cert, "NAME": name, "WEBADDR": website, "ACTIVE": 1,
        "CITY": "New York", "STALP": "NY", "ZIP": "10001",
        "FED_RSSD": str(cert * 10), **extra,
    }}


def _sam_row(uei="ABCDEF123456", website="https://www.example.com/about"):
    return {
        "entityRegistration": {
            "ueiSAM": uei, "legalBusinessName": "Example Holdings, Inc.",
            "dbaName": "Example", "cageCode": "1AB23",
            "registrationStatus": "Active",
            "registrationExpirationDate": "2027-01-01",
        },
        "coreData": {
            "entityInformation": {"entityURL": website},
            "physicalAddress": {"city": "Arlington", "stateOrProvinceCode": "VA"},
        },
    }


def test_sec_submission_binds_reported_sites_to_cik_with_provenance():
    node = parse_sec_submission({
        "cik": "320193", "name": "Apple Inc.", "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"], "website": "https://www.apple.com/",
        "investorWebsite": "https://investor.apple.com/investor-relations/",
        "formerNames": [{"name": "Apple Computer, Inc."}], "sic": "3571",
    }, observed_at=OBSERVED, source_url="https://data.sec.gov/submissions/CIK0000320193.json")

    assert node is not None
    assert node["entity_id"] == "sec_cik:0000320193"
    assert node["entity_ids"] == {"sec_cik": "0000320193"}
    assert node["aliases"] == ["Apple Computer, Inc."]
    assert {item["domain"] for item in node["domain_assertions"]} == {
        "apple.com", "investor.apple.com",
    }
    assert {item["entity_id"] for item in node["domain_assertions"]} == {
        node["entity_id"],
    }
    assert node["domain_assertions"][0]["provenance"]["observed_at"] == OBSERVED
    assert "verified" not in json.dumps(node).lower()


@pytest.mark.parametrize("website", [
    "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data",
    "http://localhost:8080/", "https://company.linkedin.com/jobs",
    "https://boards.greenhouse.io/example", "file:///etc/passwd",
    "https://user:pass@example.com/",
])
def test_domain_assertion_rejects_unsafe_or_non_official_hosts(website):
    assert domain_assertion(
        provider="sec_edgar", entity_id="sec_cik:0000000001", value=website,
        source_field="website", assertion_type="registrant_reported_website",
        source_url="https://data.sec.gov/", observed_at=OBSERVED,
    ) is None


def test_domain_assertion_requires_entity_identity():
    with pytest.raises(ValueError, match="entity_id"):
        domain_assertion(
            provider="sec_edgar", entity_id="", value="example.com",
            source_field="website", assertion_type="reported",
            source_url="https://data.sec.gov/", observed_at=OBSERVED,
        )


def test_sec_ticker_crosswalk_supports_current_array_format_and_asserts_no_domain():
    nodes = parse_sec_tickers({
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
            [320193, "Apple Inc.", "APC", "XETRA"],
            [789019, "Microsoft Corp", "MSFT", "Nasdaq"],
        ],
    }, observed_at=OBSERVED)

    assert [node["entity_id"] for node in nodes] == [
        "sec_cik:0000320193", "sec_cik:0000789019",
    ]
    assert nodes[0]["attributes"]["tickers"] == ["AAPL", "APC"]
    assert nodes[0]["domain_assertions"] == []


def test_sec_ticker_crosswalk_supports_legacy_object_format():
    nodes = parse_sec_tickers({
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }, observed_at=OBSERVED)
    assert nodes[0]["entity_id"] == "sec_cik:0000320193"
    assert nodes[0]["legal_name"] == "Apple Inc."


def test_sec_bulk_zip_is_bounded_and_keeps_per_entity_source_url():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "ignored")
        archive.writestr("CIK0000320193.json", json.dumps({
            "cik": 320193, "name": "Apple Inc.", "website": "apple.com",
        }))
        archive.writestr("CIK0000789019.json", json.dumps({
            "cik": 789019, "name": "Microsoft Corp", "website": "microsoft.com",
        }))

    nodes = parse_sec_submissions_zip(buffer.getvalue(), limit=1, observed_at=OBSERVED)
    assert len(nodes) == 1
    assert nodes[0]["entity_id"] == "sec_cik:0000320193"
    assert nodes[0]["provenance"]["bulk_source_url"].endswith("submissions.zip")
    assert nodes[0]["domain_assertions"][0]["provenance"]["source_url"].endswith(
        "CIK0000320193.json")


def test_sec_bulk_zip_rejects_unbounded_entry():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CIK0000000001.json", b"x" * 50)
    with pytest.raises(ValueError, match="entry exceeds"):
        parse_sec_submissions_zip(buffer.getvalue(), max_entry_bytes=20)


def test_fdic_parser_binds_reported_webaddr_to_certificate():
    nodes = parse_fdic_institutions({
        "meta": {"index": {"createTimestamp": "2026-08-25T17:17:35Z"}},
        "data": [_fdic_row(10004, "Ergo Bank", "www.ergobank.com")],
    }, observed_at=OBSERVED, source_url=FDIC_INSTITUTIONS_URL)

    node = nodes[0]
    assert node["entity_id"] == "fdic_cert:10004"
    assert node["entity_ids"]["fed_rssd"] == "100040"
    assert node["domain_assertions"][0]["domain"] == "ergobank.com"
    assert node["domain_assertions"][0]["entity_id"] == "fdic_cert:10004"
    assert node["attributes"]["dataset_timestamp"] == "2026-08-25T17:17:35Z"


def test_fdic_parser_requires_certificate_and_drops_social_website_assertion():
    nodes = parse_fdic_institutions({"data": [
        _fdic_row(12, "Example Bank", "https://facebook.com/example"),
        _fdic_row("", "No Identifier Bank", "example.com"),
    ]}, observed_at=OBSERVED)
    assert len(nodes) == 1
    assert nodes[0]["domain_assertions"] == []


def test_fdic_fetch_has_bounded_pagination_and_rate_interval():
    calls = []

    def handler(request):
        calls.append(request)
        offset = int(request.url.params["offset"])
        rows = (
            [_fdic_row(1, "One Bank", "one.example"),
             _fdic_row(2, "Two Bank", "two.example")]
            if offset == 0 else [_fdic_row(3, "Three Bank", "three.example")]
        )
        return httpx.Response(200, json={"meta": {"total": 3}, "data": rows})

    sleeps = []
    with _client(handler) as client:
        nodes = fetch_fdic_institutions(
            limit=3, page_size=2, max_pages=5, min_interval=0.25,
            client=client, sleep=sleeps.append,
        )
    assert [node["entity_id"] for node in nodes] == [
        "fdic_cert:1", "fdic_cert:2", "fdic_cert:3",
    ]
    assert len(calls) == 2
    assert calls[0].url.host == "api.fdic.gov"
    assert sleeps == [0.25]


def test_retry_is_bounded_and_honors_retry_after():
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"})
        return httpx.Response(200, json={
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
        })

    sleeps = []
    with _client(handler) as client:
        nodes = fetch_sec_tickers(client=client, retries=1, sleep=sleeps.append)
    assert len(nodes) == 1
    assert calls == 2
    assert sleeps == [0.1]


def test_sam_parser_binds_entity_url_to_uei():
    node = parse_sam_entity(_sam_row(), observed_at=OBSERVED)
    assert node is not None
    assert node["entity_id"] == "sam_uei:ABCDEF123456"
    assert node["aliases"] == ["Example"]
    assert node["domain_assertions"][0]["domain"] == "example.com"
    assert node["domain_assertions"][0]["entity_id"] == node["entity_id"]


def test_sam_connector_requires_key_and_never_places_it_in_url():
    with pytest.raises(RuntimeError, match="requires an API key"):
        fetch_sam_entities(["ABCDEF123456"], api_key=None)

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"entityData": [_sam_row()]})

    with _client(handler) as client:
        nodes = fetch_sam_entities(
            ["ABCDEF123456"], api_key="secret-test-key", client=client,
            min_interval=0, sleep=lambda _delay: None,
        )
    assert len(nodes) == 1
    assert requests[0].url.params["ueiSAM"] == "ABCDEF123456"
    assert requests[0].url.params["includeSections"] == "entityRegistration,coreData"
    assert requests[0].url.params["page"] == "0"
    assert requests[0].url.params["size"] == "10"
    assert requests[0].headers["x-api-key"] == "secret-test-key"
    assert "secret-test-key" not in str(requests[0].url)


def test_sam_connector_uses_documented_or_filter_and_bounded_pages():
    ueis = [f"A{number:011d}" for number in range(11)]
    requests = []

    def handler(request):
        requests.append(request)
        page = int(request.url.params["page"])
        selected = ueis[:10] if page == 0 else ueis[10:]
        return httpx.Response(200, json={
            "entityData": [_sam_row(uei=uei) for uei in selected],
            "links": {"nextLink": "https://api.sam.gov/page/1" if page == 0 else ""},
        })

    sleeps = []
    with _client(handler) as client:
        nodes = fetch_sam_entities(
            ueis, api_key="secret-test-key", client=client, batch_size=100,
            max_pages_per_batch=2, min_interval=0.25, sleep=sleeps.append,
        )
    assert len(nodes) == 11
    assert len(requests) == 2
    assert requests[0].url.params["ueiSAM"] == f"[{'~'.join(ueis)}]"
    assert [request.url.params["page"] for request in requests] == ["0", "1"]
    assert sleeps == [0.25]


def test_all_parsers_only_emit_entity_bound_assertions():
    nodes = [
        parse_sec_submission({
            "cik": 1, "name": "SEC Entity", "website": "sec-entity.example",
        }, observed_at=OBSERVED),
        *parse_fdic_institutions({
            "data": [_fdic_row(1, "FDIC Entity", "fdic-entity.example")],
        }, observed_at=OBSERVED),
        parse_sam_entity(_sam_row(website="sam-entity.example"), observed_at=OBSERVED),
    ]
    for node in nodes:
        assert node is not None
        assert node["entity_id"]
        assert node["domain_assertions"]
        assert all(assertion["entity_id"] == node["entity_id"]
                   for assertion in node["domain_assertions"])
        assert "verified" not in json.dumps(node).lower()
