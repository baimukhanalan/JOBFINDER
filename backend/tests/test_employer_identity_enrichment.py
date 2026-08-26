from backend.tools.employer_identity_enrichment import (
    _latest_employee_count, enrich_stored_bulk, enrich_structured_search,
    identity_from_stored_source, structured_row,
)


def _claim(value, *, date="", rank="normal"):
    qualifiers = {}
    if date:
        qualifiers["P585"] = [{"datavalue": {"value": {"time": date}}}]
    return {"rank": rank, "mainsnak": {"datavalue": {"value": value}},
            "qualifiers": qualifiers}


def test_employee_count_uses_latest_dated_statement_not_historical_maximum():
    entity = {"claims": {"P1128": [
        _claim({"amount": "+200000", "unit": "1"}, date="+2018-01-01T00:00:00Z"),
        _claim({"amount": "+125000", "unit": "1"}, date="+2025-01-01T00:00:00Z"),
    ]}}
    assert _latest_employee_count(entity) == 125000


def test_structured_identity_is_evidence_not_automatic_verification():
    entity = {
        "labels": {"en": {"value": "Example Corporation"}},
        "aliases": {"en": [{"value": "Example"}]},
        "claims": {
            "P856": [_claim("https://www.example.com/")],
            "P1128": [_claim({"amount": "+12000", "unit": "1"},
                              date="+2025-01-01T00:00:00Z")],
            "P452": [_claim({"id": "Q1"})],
            "P159": [_claim({"id": "Q2"})],
            "P4264": [_claim("example-company")],
        },
    }
    result = structured_row(
        {"company_id": 7, "brand_name": "Example", "legal_name": "Example Corporation",
         "employee_count_min": 10000, "employee_count_max": None,
         "employee_size_source": "E-Verify workforce range"},
        "Q99", entity, {"Q1": "customer service", "Q2": "Dallas"})
    assert result["candidate_domain"] == "example.com"
    assert result["employee_count"] == 12000
    assert result["industry"] == "customer service"
    assert result["headquarters"] == "Dallas"
    assert result["identity_confidence"] < 0.8
    assert result["domain_evidence"][0]["class"] == "structured_corporate_source"


def test_structured_identity_rejects_nonmatching_entity_name():
    entity = {"labels": {"en": {"value": "Different Company"}},
              "claims": {"P856": [_claim("https://different.example")]}}
    assert structured_row(
        {"company_id": 1, "brand_name": "Example", "legal_name": "Example Inc."},
        "Q1", entity, {}) is None


def test_structured_identity_does_not_replace_workforce_floor_with_lower_count():
    entity = {
        "labels": {"en": {"value": "Example"}},
        "claims": {"P1128": [_claim(
            {"amount": "+8000", "unit": "1"}, date="+2025-01-01T00:00:00Z")]},
    }
    result = structured_row(
        {"company_id": 1, "brand_name": "Example", "legal_name": "Example",
         "employee_count_min": 10000, "employee_count_max": None,
         "employee_size_source": "E-Verify workforce range"}, "Q1", entity, {})
    assert result["employee_count"] is None
    assert result["employee_count_min"] == 10000
    assert result["qualification_evidence"]["employee_count_conflict"] is True


def test_search_fallback_accepts_only_one_exact_normalized_entity(monkeypatch):
    row = {"company_id": 1, "brand_name": "Acme, Inc.", "legal_name": "Acme, Inc.",
           "trade_name": "Acme", "employee_count_min": 10000,
           "employee_size_source": "E-Verify workforce range"}
    entity = {"labels": {"en": {"value": "Acme"}}, "claims": {
        "P856": [_claim("https://acme.example/")],
        "P17": [_claim({"id": "Q30"})],
    }}

    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): return None
        def json(self): return self.data

    class Client:
        def get(self, _url, params):
            if params["action"] == "wbsearchentities":
                return Response({"search": [{"id": "Q7", "label": "Acme"}]})
            return Response({"entities": {"Q7": entity}})

    saved = []
    monkeypatch.setattr(
        "backend.tools.employer_identity_enrichment._list_wikidata_search_rows",
        lambda **_: [row])
    monkeypatch.setattr(
        "backend.tools.employer_identity_enrichment._persist_wikidata_search_results",
        lambda values: saved.extend(values) or {
            "matched": 1, "no_match": 0, "ambiguous": 0,
            "transient": 0, "updated": 1})
    result = enrich_structured_search(limit=1, min_interval=0, client=Client())
    assert result["matched"] == 1
    assert saved[0]["enriched"]["candidate_domain"] == "acme.example"


def test_stored_identity_keeps_missing_fields_as_explicit_gaps():
    result = identity_from_stored_source({
        "company_id": 1, "source": "gleif_lei", "source_external_id": "LEI1",
        "legal_name": "Example Legal LLC", "trade_name": "", "metadata": {},
        "brand_name": "Example Legal LLC", "employer_segment": "general",
        "headquarters_country": "US", "qualification_evidence": {},
    })
    assert result["brand_identity"]["brand_name"] is None
    assert result["employee_count"] is None
    assert result["industry"] is None
    assert result["naics_code"] is None
    assert result["headquarters"] is None
    assert set(result["gaps"]) >= {
        "brand_name", "employee_size", "industry", "naics", "headquarters"}
    assert result["status"] == "incomplete"


def test_stored_identity_labels_operational_and_registered_addresses():
    base = {
        "company_id": 1, "source_external_id": "1", "legal_name": "Example",
        "trade_name": "Example", "brand_name": "Example", "employer_segment": "general",
        "headquarters_country": "US", "qualification_evidence": {},
    }
    operational = identity_from_stored_source({
        **base, "source": "mandatory_employer",
        "metadata": {"brand_name": "Example", "operational_headquarters": {
            "city": "Austin", "region": "TX", "country": "US"}},
    })
    registered = identity_from_stored_source({
        **base, "source": "gleif_lei", "metadata": {"legal_address": {
            "addressLines": ["1 Main St"], "city": "Dover", "region": "DE",
            "country": "US"}},
    })
    assert operational["headquarters_address_type"] == "operational"
    assert operational["headquarters"] == "Austin, TX, US"
    assert registered["headquarters_address_type"] == "registered"
    assert registered["headquarters"] == "1 Main St, Dover, DE, US"

    refreshed = identity_from_stored_source({
        **base, "source": "gleif_lei", "metadata": {"source_snapshot": {
            "legal_address": {"addressLines": ["2 Main St"], "city": "Dover",
                              "region": "DE", "country": "US"}}},
    })
    assert refreshed["headquarters_address_type"] == "registered"
    assert refreshed["headquarters"] == "2 Main St, Dover, DE, US"


def test_stored_bulk_is_resumable_by_status_and_company_cursor(monkeypatch):
    calls = []
    batches = [[{
        "company_id": 7, "source": "usaspending", "source_external_id": "U1",
        "legal_name": "Example", "trade_name": "", "metadata": {},
        "brand_name": "Example", "employer_segment": "government",
        "headquarters_country": "US", "qualification_evidence": {},
    }], []]
    monkeypatch.setattr(
        "backend.tools.employer_identity_enrichment.master_db.list_stored_identity_batch",
        lambda **kwargs: calls.append(kwargs) or batches.pop(0))
    monkeypatch.setattr(
        "backend.tools.employer_identity_enrichment.master_db.update_stored_identities",
        lambda rows: len(rows))
    result = enrich_stored_bulk(batch_size=25)
    assert result == {"processed": 1, "updated": 1, "batches": 1,
                      "last_company_id": 7}
    assert calls == [
        {"limit": 25, "after_company_id": 0, "retry_incomplete": False},
        {"limit": 25, "after_company_id": 7, "retry_incomplete": False},
    ]
