"""Candidate-only search domain and downstream gate regressions."""
from __future__ import annotations

from contextlib import contextmanager

from backend.tools import employer_provisional_domain as provisional
from backend.tools.company_domain_resolver import Candidate


class Response:
    def __init__(self, url, text):
        self.url = url
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "text/html"}


class Client:
    pass


def record(company_id=7, name="Acme Logistics, Inc."):
    return {
        "company_id": company_id, "brand_name": name, "legal_name": name,
        "trade_name": name, "canonical_name": "acme logistics", "country": "US",
        "source": "dol_oflc_lca", "source_external_id": "dol-7",
    }


def test_evaluate_requires_search_homepage_identity_and_domain_overlap(monkeypatch):
    monkeypatch.setattr(provisional, "search_candidates", lambda *_args, **_kwargs: [
        Candidate("https://acmelogistics.com/", "duckduckgo_html", search_rank=1)])
    monkeypatch.setattr(provisional, "_get", lambda *_args, **_kwargs: Response(
        "https://acmelogistics.com/", "<title>Acme Logistics</title><h1>Acme Logistics</h1>"))
    monkeypatch.setattr(provisional, "enrich_company", lambda *_args, **_kwargs: {
        "careers_url": "https://acmelogistics.com/careers", "ats": "lever",
        "ats_slug": "acme", "ats_url": "https://jobs.lever.co/acme",
        "careers_confidence": 0.9,
    })
    result = provisional.evaluate(
        record(), client=Client(), limiter=provisional.RequestLimiter(0), retries=0)
    assert result["status"] == "accepted"
    assert result["candidate_domain"] == "acmelogistics.com"
    assert result["domain_evidence"][0]["class"] == \
        "provisional_search_official_homepage"
    assert result["domain_evidence"][0]["status"] == "provisional"
    assert "domain_verified" not in result


def test_evaluate_rejects_unrelated_homepage_even_for_search_rank_one(monkeypatch):
    monkeypatch.setattr(provisional, "search_candidates", lambda *_args, **_kwargs: [
        Candidate("https://acmelogistics.com/", "bing_html", search_rank=1)])
    monkeypatch.setattr(provisional, "_get", lambda *_args, **_kwargs: Response(
        "https://acmelogistics.com/", "<title>Unrelated municipal portal</title>"))
    result = provisional.evaluate(
        record(), client=Client(), limiter=provisional.RequestLimiter(0), retries=0)
    assert result["status"] == "no_match"
    assert result["reason"] == "homepage_identity_or_domain_overlap_failed"


def test_empty_search_is_retryable_not_final_no_match(monkeypatch):
    calls = []
    monkeypatch.setattr(provisional, "search_candidates",
                        lambda *_args, **_kwargs: calls.append(1) or [])
    monkeypatch.setattr(provisional.time, "sleep", lambda _seconds: None)
    result = provisional.evaluate(
        record(), client=Client(), limiter=provisional.RequestLimiter(0), retries=2)
    assert result["status"] == "transient"
    assert len(calls) == 3


def test_persist_never_promotes_identity_domain_cohort_monitoring_or_queue(monkeypatch):
    calls = []
    class Cursor:
        rowcount = 1
        def execute(self, sql, params): calls.append((sql, params))
        def close(self): pass
    class Connection:
        def cursor(self): return Cursor()

    @contextmanager
    def fake_conn(): yield Connection()
    monkeypatch.setattr(provisional.company_db, "conn", fake_conn)
    result = provisional.persist([{
        "company_id": 7, "status": "accepted", "attempted_at": "now",
        "candidate_domain": "acmelogistics.com",
        "domain_evidence": [{"class": "provisional_search_official_homepage"}],
        "careers_url": "https://acmelogistics.com/careers", "ats": "lever",
        "ats_slug": "acme", "ats_url": "https://jobs.lever.co/acme",
        "careers_confidence": 0.9,
    }])
    assert result["accepted"] == result["updated"] == 1
    sql = " ".join(statement for statement, _params in calls).casefold()
    assert "candidate_domain" in sql and "provisional_domain" in sql
    assert "careers_url" in sql and "ats_slug" in sql
    assert "domain_verified=true" not in sql
    assert "identity_status=" not in sql
    assert "hiring_cohort_status=" not in sql
    assert "monitoring_status=" not in sql
    assert "application_queue" not in sql and "remote_applications" not in sql


def test_run_commits_each_checkpoint_and_caps_workers(monkeypatch):
    rows = [record(1), record(2), record(3)]
    monkeypatch.setattr(provisional, "list_candidates", lambda **_: rows)
    monkeypatch.setattr(provisional, "evaluate", lambda row, **_: {
        "company_id": row["company_id"], "status": "no_match", "reason": "none",
        "attempted_at": "now",
    })
    saved = []
    monkeypatch.setattr(provisional, "persist", lambda values: (
        saved.append([item["company_id"] for item in values]) or {
            "accepted": 0, "no_match": len(values), "ambiguous": 0,
            "transient": 0, "updated": len(values), "job_discovery_ready": 0,
        }))
    result = provisional.run(
        limit=3, workers=99, checkpoint_size=2, min_interval=0)
    assert result["workers"] == 4
    assert [sorted(batch) for batch in saved] == [[1, 2], [3]]
    assert result["updated"] == 3


def test_retry_loader_selects_only_transient_checkpoint(monkeypatch):
    calls = []
    class Cursor:
        def execute(self, sql, params): calls.append((sql, params))
        def fetchall(self): return []
    @contextmanager
    def fake_cur(*_args, **_kwargs): yield Cursor()
    monkeypatch.setattr(provisional.company_db, "_cur", fake_cur)
    provisional.list_candidates(limit=10, retry_transient=True)
    assert "provisional_domain,status" in calls[0][0]
    assert "='transient'" in calls[0][0]
