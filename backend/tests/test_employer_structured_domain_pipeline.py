from contextlib import contextmanager

from backend.tools import employer_structured_domain_pipeline as pipeline


def row(**extra):
    value = {"company_id": 1, "source": "wikidata_employer",
             "source_url": "https://www.wikidata.org/wiki/Q1",
             "source_observed_at": "2026-08-26T00:00:00Z",
             "legal_name": "Exact Company, Inc.", "brand_name": "Exact Company",
             "trade_name": "Exact Company", "domain": "www.exact.example",
             "external_ids": {},
             "metadata": {"wikidata_qid": "Q1", "official_website_property": "P856"}}
    value.update(extra); return value


def test_wikidata_p856_exact_entity_is_first_factor_not_verification():
    factor = pipeline.wikidata_p856_candidate(row())
    assert factor["class"] == "structured_corporate_source"
    assert factor["entity_id"] == "wikidata_qid:Q1"
    assert factor["candidate_domain"] == "exact.example"
    assert "verified" not in factor


def test_wikidata_requires_qid_bound_source_url_and_p856():
    assert pipeline.wikidata_p856_candidate(row(source_url="https://wikidata/Q2")) is None
    assert pipeline.wikidata_p856_candidate(row(
        metadata={"wikidata_qid": "Q1"})) is None


def test_entity_assertion_must_bind_same_durable_id():
    linked = row(external_ids={"fdic_cert": "123"})
    node = {"entity_id": "fdic_cert:123", "domain_assertions": [{
        "entity_id": "fdic_cert:123", "domain": "bank.example",
        "assertion_type": "institution_reported_primary_website",
        "provenance": {"source_url": "https://fdic.gov", "observed_at": "now"}}]}
    assert pipeline.entity_assertion_candidate(
        linked, node, id_key="fdic_cert", provider="fdic_bankfind")["candidate_domain"] == \
        "bank.example"
    node["entity_id"] = "fdic_cert:999"
    assert pipeline.entity_assertion_candidate(
        linked, node, id_key="fdic_cert", provider="fdic_bankfind") is None


def test_conflicting_authoritative_domains_quarantine():
    linked = row(external_ids={"fdic_cert": "123"})
    def fdic(_cert):
        return {"proposed_domain_evidence": {"entity_id": "fdic_cert:123",
                "domain": "different.example", "provenance": {}}}
    result = pipeline.resolve_authoritative_candidates(linked, fdic_fetcher=fdic)
    assert result["status"] == "quarantine"
    assert result["domains"] == ["different.example", "exact.example"]


def test_search_is_candidate_only_and_never_verification_eligible():
    candidate = pipeline.search_candidate(domain="result.example", source_url="search")
    assert candidate["class"] == "search_candidate"
    assert candidate["verification_eligible"] is False


class Response:
    url = "https://exact.example/"
    text = "<title>Exact Company, Inc.</title><main>ignored</main>"


def test_live_second_factor_uses_strict_identity_context(monkeypatch):
    monkeypatch.setattr(pipeline, "_get", lambda *_args, **_kwargs: Response())
    result = pipeline.verify_live_record(
        row(candidate_domain="exact.example"), client=object(),
        limiter=pipeline.RequestLimiter(0.1))
    assert result["status"] == "verified"
    Response.text = "<main>Exact Company, Inc.</main>"
    result = pipeline.verify_live_record(
        row(candidate_domain="exact.example"), client=object(),
        limiter=pipeline.RequestLimiter(0.1))
    assert result["status"] == "quarantine"


def test_persist_proposal_never_sets_domain_verified_or_discovery_domain(monkeypatch):
    calls = []
    class Cursor:
        rowcount = 1
        def execute(self, sql, params): calls.append((sql, params))
    @contextmanager
    def fake_cur(_dict_rows=True): yield Cursor()
    monkeypatch.setattr(pipeline.company_db, "_cur", fake_cur)
    result = pipeline.persist_proposals([{
        "company_id": 1, "status": "proposed", "candidate_domain": "exact.example",
        "factors": [{"class": "structured_corporate_source"}]}])
    assert result["proposed"] == result["updated"] == 1
    assert "domain_verified" not in calls[0][0]
    assert "company_discovery" not in calls[0][0]


def test_verified_apply_writes_domain_only_not_careers_jobs_or_ats(monkeypatch):
    calls = []
    class Cursor:
        rowcount = 1
        def execute(self, sql, params): calls.append((sql, params))
    @contextmanager
    def fake_cur(_dict_rows=True): yield Cursor()
    monkeypatch.setattr(pipeline.company_db, "_cur", fake_cur)
    result = pipeline.persist_verifications([{
        "company_id": 1, "status": "verified", "candidate_domain": "exact.example",
        "identity": {"final_url": "https://exact.example/", "matched_name": "Exact Company",
                     "context_type": "title", "context_excerpt": "Exact Company"}}])
    assert result["verified"] == 1
    sql = " ".join(statement for statement, _params in calls).casefold()
    assert "domain_verified=true" in sql
    assert all(word not in sql for word in ("careers", "ats", "job"))


def test_transient_result_is_checkpointed_as_retryable_not_final(monkeypatch):
    monkeypatch.setattr(pipeline, "load_verification_rows", lambda **_: [row()])
    monkeypatch.setattr(pipeline, "verify_live_record",
                        lambda *_args, **_kwargs: {"company_id": 1, "status": "transient"})
    captured = []
    monkeypatch.setattr(pipeline, "persist_verifications",
                        lambda results: captured.extend(results) or
                        {"verified": 0, "quarantine": 0, "transient": 1, "updated": 1})
    result = pipeline.verify_structured_domains(limit=1, workers=1)
    assert result["transient"] == 1
    assert captured == [{"company_id": 1, "status": "transient"}]
