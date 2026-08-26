"""Concurrency, retry and checkpoint tests for structured Wikidata enrichment."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

import httpx
import pytest

from backend.tools import employer_identity_enrichment as enrichment


def candidate(company_id: int, name: str | None = None) -> dict:
    name = name or f"Exact Company {company_id}"
    return {
        "company_id": company_id, "brand_name": name, "legal_name": name,
        "trade_name": name, "employee_count": None, "employee_count_min": 10000,
        "employee_count_max": None, "employee_size_source": "range",
        "industry": None, "headquarters": None,
    }


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.request = httpx.Request("GET", "https://www.wikidata.org/w/api.php")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("provider error", request=self.request,
                                        response=httpx.Response(
                                            self.status_code, request=self.request))

    def json(self):
        return self._payload


def test_wikidata_request_retries_429_with_bounded_backoff():
    class Client:
        def __init__(self):
            self.responses = [Response(429, headers={"Retry-After": "0.01"}),
                              Response(payload={"entities": {}})]
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    sleeps = []
    client = Client()
    payload = enrichment._wikidata_json(
        client, {"action": "wbgetentities"},
        limiter=enrichment.WikidataRateLimiter(0), retries=2,
        sleep=lambda seconds: sleeps.append(seconds))
    assert payload == {"entities": {}}
    assert client.calls == 2
    assert sleeps == [0.01]


def test_wikidata_request_stops_after_bounded_5xx_retries():
    class Client:
        calls = 0
        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response(503)

    client = Client()
    with pytest.raises(RuntimeError, match="bounded retries"):
        enrichment._wikidata_json(
            client, {"action": "wbgetentities"},
            limiter=enrichment.WikidataRateLimiter(0), retries=2,
            sleep=lambda _seconds: None)
    assert client.calls == 3


def test_wikidata_request_does_not_retry_nonretryable_4xx():
    class Client:
        calls = 0
        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response(400)

    client = Client()
    with pytest.raises(RuntimeError, match="bounded retries"):
        enrichment._wikidata_json(
            client, {"action": "wbgetentities"},
            limiter=enrichment.WikidataRateLimiter(0), retries=3,
            sleep=lambda _seconds: None)
    assert client.calls == 1


def test_enrichment_caps_workers_and_commits_each_checkpoint_batch(monkeypatch):
    rows = [candidate(index) for index in range(1, 6)]
    monkeypatch.setattr(enrichment, "_list_wikidata_rows", lambda **_: rows)

    class Client:
        def get(self, *_args, **_kwargs):
            return Response(payload={"entities": {}})

    persisted = []
    pool_sizes = []
    class RecordingExecutor(RealThreadPoolExecutor):
        def __init__(self, max_workers=None, *args, **kwargs):
            pool_sizes.append(max_workers)
            super().__init__(max_workers=max_workers, *args, **kwargs)

    monkeypatch.setattr(enrichment, "ThreadPoolExecutor", RecordingExecutor)
    def save(results):
        persisted.append(list(results))
        return {"matched": 0, "no_match": len(results), "transient": 0,
                "updated": len(results)}
    monkeypatch.setattr(enrichment, "_persist_wikidata_results", save)
    result = enrichment.enrich_structured(
        limit=5, workers=99, checkpoint_size=2, min_interval=0,
        retries=0, client=Client())
    assert result["workers"] == 4
    assert result["batches"] == 3
    assert result["processed"] == result["no_match"] == result["updated"] == 5
    assert [len(batch) for batch in persisted] == [2, 2, 1]
    assert pool_sizes and max(pool_sizes) == 4


def test_exact_label_with_official_website_creates_candidate_only(monkeypatch):
    rows = [candidate(1, "Acme Corporation")]
    monkeypatch.setattr(enrichment, "_list_wikidata_rows", lambda **_: rows)

    class Client:
        def get(self, _url, params):
            assert params["action"] == "wbgetentities"
            return Response(payload={"entities": {"Q1": {
                "labels": {"en": {"value": "Acme Corporation"}},
                "claims": {"P856": [{"mainsnak": {"datavalue": {
                    "value": "https://www.acme.example/careers"}}}]},
            }}})

    captured = []
    monkeypatch.setattr(enrichment, "_persist_wikidata_results",
                        lambda results: captured.extend(results) or {
                            "matched": 1, "no_match": 0, "transient": 0, "updated": 1})
    result = enrichment.enrich_structured(
        limit=1, min_interval=0, retries=0, client=Client())
    assert result["matched"] == 1
    assert captured[0]["status"] == "matched"
    assert captured[0]["enriched"]["candidate_domain"] == "acme.example"
    assert "domain_verified" not in captured[0]["enriched"]


def test_exact_entity_without_p856_is_checkpointed_as_no_candidate(monkeypatch):
    rows = [candidate(1, "Acme Corporation")]
    monkeypatch.setattr(enrichment, "_list_wikidata_rows", lambda **_: rows)

    class Client:
        def get(self, _url, params):
            return Response(payload={"entities": {"Q1": {
                "labels": {"en": {"value": "Acme Corporation"}}, "claims": {},
            }}})

    captured = []
    monkeypatch.setattr(enrichment, "_persist_wikidata_results",
                        lambda results: captured.extend(results) or {
                            "matched": 0, "no_match": 1, "transient": 0, "updated": 1})
    enrichment.enrich_structured(limit=1, min_interval=0, retries=0, client=Client())
    assert captured[0]["status"] == "no_match"
    assert captured[0]["reason"] == "official_website_missing"


def test_only_exact_normalized_label_is_matched(monkeypatch):
    rows = [candidate(1, "Acme Corporation")]
    monkeypatch.setattr(enrichment, "_list_wikidata_rows", lambda **_: rows)

    class Client:
        def get(self, _url, params):
            if params.get("props") == "labels":
                return Response(payload={"entities": {}})
            return Response(payload={"entities": {"Q1": {
                "labels": {"en": {"value": "Acme Holdings"}}, "claims": {}}}})

    captured = []
    monkeypatch.setattr(enrichment, "_persist_wikidata_results",
                        lambda results: captured.extend(results) or {
                            "matched": 0, "no_match": 1, "transient": 0, "updated": 1})
    enrichment.enrich_structured(
        limit=1, min_interval=0, retries=0, client=Client())
    assert captured[0]["status"] == "no_match"
    assert captured[0]["reason"] == "no_exact_entity"


def test_successful_batches_remain_saved_if_later_batch_stops(monkeypatch):
    rows = [candidate(index) for index in range(1, 5)]
    monkeypatch.setattr(enrichment, "_list_wikidata_rows", lambda **_: rows)

    class Client:
        def get(self, *_args, **_kwargs):
            return Response(payload={"entities": {}})

    saved = []
    def save(results):
        saved.append([item["company_id"] for item in results])
        if len(saved) == 2:
            raise KeyboardInterrupt()
        return {"matched": 0, "no_match": len(results), "transient": 0,
                "updated": len(results)}
    monkeypatch.setattr(enrichment, "_persist_wikidata_results", save)
    with pytest.raises(KeyboardInterrupt):
        enrichment.enrich_structured(
            limit=4, checkpoint_size=2, min_interval=0, retries=0, client=Client())
    assert saved[0] == [1, 2]


def test_persistence_writes_candidate_evidence_and_resume_checkpoint_only(monkeypatch):
    calls = []
    class Cursor:
        rowcount = 1
        def execute(self, sql, params):
            calls.append((sql, params))

    @contextmanager
    def fake_cur(*_args, **_kwargs):
        yield Cursor()

    monkeypatch.setattr(enrichment.company_db, "_cur", fake_cur)
    enriched = {
        "company_id": 1, "candidate_domain": "exact.example",
        "employee_count": None, "employee_count_min": 10000,
        "employee_count_max": None, "employee_size_source": "range",
        "industry": None, "headquarters": None, "linkedin_url": None,
        "identity_confidence": 0.62,
        "domain_evidence": [{"class": "structured_corporate_source",
                             "provider": "wikidata",
                             "candidate_domain": "exact.example"}],
        "qualification_evidence": {"wikidata_entity": "Q1",
                                   "structured_name_match": True},
    }
    result = enrichment._persist_wikidata_results([
        {"company_id": 1, "status": "matched", "enriched": enriched},
        {"company_id": 2, "status": "transient", "reason": "provider"},
    ])
    assert result == {"matched": 1, "no_match": 0, "transient": 1, "updated": 2}
    sql = " ".join(statement for statement, _params in calls).casefold()
    assert "candidate_domain" in sql and "domain_evidence" in sql
    assert "wikidata_enrichment" in sql
    assert "domain_verified" not in sql
    assert "company_discovery" not in sql


def test_retry_loader_selects_only_transient_checkpoints(monkeypatch):
    calls = []
    class Cursor:
        def execute(self, sql, params): calls.append((sql, params))
        def fetchall(self): return []

    @contextmanager
    def fake_cur(*_args, **_kwargs): yield Cursor()
    monkeypatch.setattr(enrichment.company_db, "_cur", fake_cur)
    enrichment._list_wikidata_rows(limit=10, retry_transient=True)
    assert "wikidata_enrichment,status" in calls[0][0]
    assert "='transient'" in calls[0][0]
