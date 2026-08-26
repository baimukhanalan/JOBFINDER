import json
from contextlib import contextmanager

from backend.tools import company_discovery as cli
from backend.tools import company_discovery_db as db
from backend.tools.company_domain_resolver import (
    Candidate,
    RateLimiter,
    _allowed_candidate,
    _decode_ddg_url,
    bulk_mediawiki_candidates,
    bulk_wikidata_candidates,
    resolve_company,
    verify_candidate,
    wikidata_candidates,
)


class Response:
    def __init__(self, url, *, payload=None, text="", status=200,
                 content_type="text/html"):
        self.url = url
        self._payload = payload
        self.text = text
        self.status_code = status
        self.headers = {"content-type": content_type}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            return Response(url, status=404)
        return self.responses.pop(0)


class SparqlClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_wikidata_requires_exact_entity_name_and_official_website_claim():
    client = Client([
        Response("wikidata", payload={"search": [
            {"id": "Q1", "label": "Acme Corporation"},
            {"id": "Q2", "label": "Acme Consulting"},
        ]}, content_type="application/json"),
        Response("wikidata", payload={"entities": {"Q1": {
            "labels": {"en": {"value": "Acme Corporation"}},
            "claims": {"P856": [{"mainsnak": {"datavalue": {
                "value": "https://www.acme.example/"}}}]},
        }}}, content_type="application/json"),
    ])
    result = wikidata_candidates(
        {"legal_name": "ACME CORP"}, client, RateLimiter(0))
    assert result == [Candidate("https://www.acme.example/", "wikidata_p856",
                                "Q1", "Acme Corporation")]


def test_bulk_sparql_maps_normalized_exact_label_to_record():
    response = Response("sparql", payload={"results": {"bindings": [{
        "input": {"value": "acme"},
        "item": {"value": "http://www.wikidata.org/entity/Q1"},
        "label": {"value": "Acme"},
        "website": {"value": "https://acme.example/"},
    }]}}, content_type="application/sparql-results+json")
    client = SparqlClient(response)
    result, completed, failed = bulk_wikidata_candidates(
        [{"id": 7, "legal_name": "Acme Corp", "canonical_name": "acme"}],
        client=client, limiter=RateLimiter(0), batch_size=25)
    assert completed == {7}
    assert failed == set()
    assert result[7] == [Candidate(
        "https://acme.example/", "wikidata_sparql_p856", "Q1", "Acme")]
    query = client.calls[0][1]["data"]["query"]
    assert "VALUES ?label" in query
    assert '"Acme"@en' in query
    assert "LCASE" not in query


def test_bulk_sparql_failure_is_retryable_not_a_negative_match():
    client = SparqlClient(Response("sparql", payload={}, status=503))
    result, completed, failed = bulk_wikidata_candidates(
        [{"id": 7, "legal_name": "Acme Corp"}], client=client,
        limiter=RateLimiter(0), batch_size=25, retries=0)
    assert result == {7: []}
    assert completed == set()
    assert failed == {7}


def test_mediawiki_bulk_maps_exact_alias_and_p856():
    payload = {"entities": {"Q7": {
        "labels": {"en": {"value": "Different Display Name"}},
        "aliases": {"en": [{"value": "Acme Corporation"}]},
        "claims": {"P856": [{"mainsnak": {"datavalue": {
            "value": "https://acme.example/"}}}]},
    }}}
    client = Client([Response("wikidata", payload=payload,
                              content_type="application/json")])
    result, completed, failed = bulk_mediawiki_candidates(
        [{"id": 7, "legal_name": "ACME CORP", "canonical_name": "acme"}],
        client=client, limiter=RateLimiter(0), batch_size=25)
    assert completed == {7}
    assert failed == set()
    assert result[7] == [Candidate(
        "https://acme.example/", "wikidata_api_p856", "Q7", "Acme Corporation")]


def test_mediawiki_api_error_leaves_chunk_retryable():
    client = Client([Response("wikidata", payload={"error": {"code": "toomanyvalues"}},
                              content_type="application/json")])
    result, completed, failed = bulk_mediawiki_candidates(
        [{"id": 8, "legal_name": "Acme Corp"}], client=client,
        limiter=RateLimiter(0), batch_size=25, retries=0)
    assert result == {8: []}
    assert completed == set()
    assert failed == {8}


def test_mediawiki_bulk_keeps_duplicate_normalized_names_retryable():
    payload = {"entities": {"Q7": {
        "labels": {"en": {"value": "Acme Corporation"}},
        "claims": {"P856": [{"mainsnak": {"datavalue": {
            "value": "https://acme.example/"}}}]},
    }}}
    client = Client([Response("wikidata", payload=payload,
                              content_type="application/json")])
    rows = [
        {"id": 7, "legal_name": "ACME CORP", "canonical_name": "acme"},
        {"id": 8, "legal_name": "Acme Corporation", "canonical_name": "acme"},
    ]
    result, completed, failed = bulk_mediawiki_candidates(
        rows, client=client, limiter=RateLimiter(0), batch_size=25)
    assert result == {7: [], 8: []}
    assert completed == set()
    assert failed == {7, 8}


def test_duplicate_name_guard_spans_resolver_chunks():
    rows = [{"id": i, "legal_name": f"Unique {i}"} for i in range(1, 102)]
    rows[0]["legal_name"] = "ACME CORP"
    rows[100]["legal_name"] = "Acme Corporation"
    assert cli._ambiguous_company_ids(rows) == {1, 101}


def test_search_candidate_needs_strong_homepage_name_evidence():
    weak = Client([Response("https://unrelated.example/", text="<title>Business directory</title>")])
    assert verify_candidate(
        {"legal_name": "Lockheed Martin Corp"},
        Candidate("https://unrelated.example/", "duckduckgo_html", search_rank=1),
        weak, RateLimiter(0),
    ) is None

    strong = Client([Response(
        "https://lockheedmartin.example/",
        text="<title>Lockheed Martin | Global aerospace company</title>",
    )])
    result = verify_candidate(
        {"legal_name": "Lockheed Martin Corp"},
        Candidate("https://lockheedmartin.example/", "duckduckgo_html", search_rank=1),
        strong, RateLimiter(0),
    )
    assert result["domain"] == "lockheedmartin.example"
    assert result["domain_confidence"] >= 0.88


def test_candidate_safety_excludes_private_social_and_ats_hosts():
    assert not _allowed_candidate("http://127.0.0.1/admin")
    assert not _allowed_candidate("https://linkedin.com/company/acme")
    assert not _allowed_candidate("https://jobs.lever.co/acme")
    assert _allowed_candidate("https://acme.example/")
    assert _decode_ddg_url(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example%2F") == \
        "https://acme.example/"


def test_full_resolution_preserves_structured_provenance_and_enriches_ats():
    search = {"search": [{"id": "Q42", "label": "Acme Corporation"}]}
    entity = {"entities": {"Q42": {
        "labels": {"en": {"value": "Acme Corporation"}},
        "claims": {"P856": [{"mainsnak": {"datavalue": {
            "value": "https://acme.example/"}}}]},
    }}}
    home = '<title>Acme Corporation</title><a href="/careers">Careers</a>'
    careers = '<a href="https://jobs.ashbyhq.com/acme">Jobs</a>'
    client = Client([
        Response("wikidata", payload=search, content_type="application/json"),
        Response("wikidata", payload=entity, content_type="application/json"),
        Response("https://acme.example/", text=home),
        Response("https://acme.example/", text=home),
        Response("https://acme.example/careers", text=careers),
    ])
    result = resolve_company(
        {"legal_name": "ACME CORP", "provenance": {"source": "test"}},
        client=client, limiter=RateLimiter(0),
    )
    assert result["domain"] == "acme.example"
    assert result["careers_url"] == "https://acme.example/careers"
    assert result["ats"] == "ashby"
    assert result["domain_resolution"]["provider_id"] == "Q42"
    assert result["domain_resolution"]["result"] == "resolved"
    assert result["domain_resolution"]["attempted_at"]
    assert result["provenance"]["source"] == "test"


def test_cli_domain_run_reads_only_missing_domain_api(monkeypatch, capsys):
    row = {"id": 7, "legal_name": "Acme Corp", "domain": None}
    monkeypatch.setattr(cli.company_db, "list_without_domain", lambda **kwargs: [row])
    monkeypatch.setattr(cli, "resolve_company", lambda *args, **kwargs: {
        "domain": "acme.example", "domain_confidence": 0.93,
        "domain_resolution": {"resolver": "wikidata_p856"},
    })
    monkeypatch.setattr(cli.company_db, "update_resolved_company",
                        lambda company_id, result: company_id == 7)
    monkeypatch.setattr(cli.company_db, "catalog_companies",
                        lambda: (_ for _ in ()).throw(AssertionError("catalog must not be read")))
    assert cli.main(["resolve-domains", "--limit", "1", "--workers", "1",
                     "--min-interval", "0.25"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["selected"] == output["resolved"] == output["updated"] == 1


def test_unresolved_attempt_is_persisted_and_resumable(monkeypatch, capsys):
    row = {"id": 11, "legal_name": "No Website LLC", "domain": None}
    monkeypatch.setattr(cli.company_db, "list_without_domain", lambda **kwargs: [row])

    def unresolved(*args, **kwargs):
        kwargs["attempt_out"].update({
            "attempted_at": "2026-08-26T00:00:00+00:00",
            "resolver": "wikidata_p856", "result": "unresolved",
        })
        return None

    stored = []
    monkeypatch.setattr(cli, "resolve_company", unresolved)
    monkeypatch.setattr(cli.company_db, "record_domain_resolution_attempts",
                        lambda rows: stored.extend(rows) or len(rows))
    assert cli.main(["resolve-domains", "--limit", "1", "--workers", "1",
                     "--min-interval", "0.25", "--no-search-fallback"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["resolved"] == output["updated"] == 0
    assert output["unresolved_recorded"] == 1
    assert stored == [(11, {
        "attempted_at": "2026-08-26T00:00:00+00:00",
        "resolver": "wikidata_p856", "result": "unresolved",
    })]


def test_missing_domain_query_skips_attempted_unless_retry(monkeypatch):
    class Cursor:
        calls = []

        def execute(self, sql, args):
            self.calls.append((sql, args))

        def fetchall(self):
            return []

    cursor = Cursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    db.list_without_domain(limit=5)
    db.list_without_domain(limit=5, retry_attempted=True)
    assert "? 'domain_resolution'" in cursor.calls[0][0]
    assert "? 'domain_resolution'" not in cursor.calls[1][0]


def test_negative_attempt_bulk_update_keeps_domain_guard(monkeypatch):
    class Cursor:
        rowcount = 2
        call = None

        def executemany(self, sql, values):
            self.call = (sql, values)

    cursor = Cursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    assert db.record_domain_resolution_attempts([
        (1, {"attempted_at": "now", "resolver": "wikidata_p856", "result": "unresolved"}),
        (2, {"attempted_at": "now", "resolver": "wikidata_p856", "result": "unresolved"}),
    ]) == 2
    assert "NULLIF(BTRIM(domain), '') IS NULL" in cursor.call[0]


def test_update_resolved_company_is_non_overwriting_and_keeps_evidence(monkeypatch):
    class Cursor:
        rowcount = 1
        call = None

        def execute(self, sql, args):
            self.call = (sql, args)

    cursor = Cursor()

    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor

    monkeypatch.setattr(db, "_cur", fake_cur)
    assert db.update_resolved_company(9, {
        "domain": "https://www.acme.example/about", "domain_confidence": 0.93,
        "domain_resolution": {"resolver": "wikidata_p856", "provider_id": "Q42"},
    })
    sql, args = cursor.call
    assert "NULLIF(BTRIM(domain), '') IS NULL" in sql
    assert "domain_resolution" in sql
    assert args[0] == "acme.example"
