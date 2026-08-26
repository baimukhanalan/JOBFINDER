import httpx

from backend.tools import employer_source_snapshot_refresh as refresh


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gleif_refresh_keeps_registered_and_headquarters_types_separate(monkeypatch):
    row = {"company_id": 1, "source_external_id": "LEI1", "metadata": {}}
    monkeypatch.setattr(refresh, "list_refresh_candidates", lambda *a, **k: [row])
    saved = []
    monkeypatch.setattr(refresh, "save_snapshots",
                        lambda rows: saved.extend(rows) or len(rows))

    def handler(request):
        assert request.url.params["filter[lei]"] == "LEI1"
        return httpx.Response(200, json={"data": [{"id": "LEI1", "attributes": {
            "lei": "LEI1", "entity": {
                "legalName": {"name": "Acme LLC"},
                "legalAddress": {"country": "US", "region": "US-DE"},
                "headquartersAddress": {"country": "US", "region": "US-TX"},
                "status": "ACTIVE", "category": "GENERAL",
            }}}]})

    with _client(handler) as client:
        result = refresh.refresh_source("gleif_lei", limit=1, min_interval=0,
                                        client=client)
    assert result["success"] == 1
    assert [item["address_type"] for item in saved[0]["snapshot"]["addresses"]] == [
        "registered", "headquarters"]
    assert all(item["address_type"] != "operational"
               for item in saved[0]["snapshot"]["addresses"])


def test_usaspending_refresh_resolves_exact_id_and_saves_official_profile(monkeypatch):
    row = {"company_id": 2, "source_external_id": "UEI1",
           "metadata": {"uei": "UEI1"}}
    monkeypatch.setattr(refresh, "list_refresh_candidates", lambda *a, **k: [row])
    saved = []
    monkeypatch.setattr(refresh, "save_snapshots",
                        lambda rows: saved.extend(rows) or len(rows))

    def handler(request):
        if request.url.path.endswith("/api/v2/recipient/"):
            return httpx.Response(200, json={"results": [
                {"id": "hash-C", "uei": "UEI1", "recipient_level": "C", "amount": 5},
                {"id": "hash-R", "uei": "UEI1", "recipient_level": "R", "amount": 4},
            ]})
        assert request.url.path.endswith("/api/v2/search/spending_by_award/")
        return httpx.Response(200, json={"results": [{
            "Recipient Name": "Fuzzy", "Recipient UEI": "OTHER",
            "recipient_id": "other-R", "Recipient Location": {},
        }, {
            "Recipient Name": "Acme", "Recipient UEI": "UEI1",
            "recipient_id": "hash-R", "Recipient Location": {
                "city_name": "Austin", "state_code": "TX",
                "location_country_code": "USA"},
        }]})

    with _client(handler) as client:
        result = refresh.refresh_source("usaspending", limit=1, min_interval=0,
                                        client=client)
    assert result["success"] == 1
    snapshot = saved[0]["snapshot"]
    assert snapshot["recipient_id"] == "hash-R"
    assert snapshot["business_types"] == []
    assert snapshot["business_types_gap"] == \
        "official_award_search_does_not_expose_business_types"
    assert snapshot["recipient_location"]["address_type"] == "recipient_location"
    assert snapshot["fallback_method"] == "official_spending_by_award_exact_uei"
    assert "employee_count" not in snapshot and "naics" not in snapshot


def test_refresh_limit_is_hard_bounded(monkeypatch):
    seen = []
    monkeypatch.setattr(refresh, "list_refresh_candidates",
                        lambda source, **kwargs: seen.append(kwargs["limit"]) or [])
    result = refresh.refresh_source("gleif_lei", limit=5000, min_interval=0,
                                    client=object())
    assert seen == [500]
    assert result["selected"] == 0


def test_run_until_done_reports_durable_company_checkpoint(monkeypatch):
    calls = []
    results = iter([
        {"selected": 2, "updated": 2, "success": 2, "errors": 0,
         "last_company_id": 20},
        {"selected": 1, "updated": 1, "success": 1, "errors": 0,
         "last_company_id": 30},
        {"selected": 0, "updated": 0, "success": 0, "errors": 0,
         "last_company_id": 30},
    ])
    monkeypatch.setattr(refresh, "refresh_source",
                        lambda *args, **kwargs: next(results))
    result = refresh.run_until_done(
        "gleif_lei", run_cap=10, batch_size=2,
        progress=lambda value: calls.append(value))
    assert result["selected"] == 3
    assert result["checkpoint_company_id"] == 30
    assert result["done"] is True
    assert calls[-1]["checkpoint_company_id"] == 30


def test_gleif_multi_filter_chunks_at_100_ids():
    sizes = []

    def handler(request):
        leis = request.url.params["filter[lei]"].split(",")
        sizes.append(len(leis))
        return httpx.Response(200, json={"data": [{
            "id": lei, "attributes": {"lei": lei, "entity": {
                "legalName": {"name": f"Company {lei}"},
                "legalAddress": {"country": "US"},
                "status": "ACTIVE", "category": "GENERAL",
            }}} for lei in leis]})

    rows = [{"source_external_id": f"LEI{i:017d}"} for i in range(101)]
    with _client(handler) as client:
        result = refresh._gleif_snapshots(rows, client)
    assert sizes == [100, 1]
    assert len(result) == 101
