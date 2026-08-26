import json
from contextlib import contextmanager

import pytest

from backend.tools import mandatory_employer_domain_verification as verification


def authoritative(domain="example.com"):
    return {"provider": "mandatory_authoritative",
            "class": "authoritative_first_factor",
            "assertion": "reported_official_domain", "domain": domain,
            "observed_at": "2026-08-26", "sources": [{"url": "issuer"}]}


def passed():
    return {"company_id": 1, "domain": "example.com",
            "first_factor": authoritative(), "identity": {
                "passed": True, "proposed_domain": "example.com",
                "final_url": "https://example.com/", "matched_name": "Example Corp",
                "context_type": "title", "context_excerpt": "Example Corp"}}


class Cursor:
    rowcount = 1

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


def test_assertion_requires_exact_provider_class_assertion_and_domain():
    row = {"domain": "example.com", "domain_evidence": [authoritative()]}
    assert verification._authoritative_assertion(row)
    for field, value in (("provider", "other"), ("class", "other"),
                         ("assertion", "other"), ("domain", "other.com")):
        bad = authoritative()
        bad[field] = value
        assert verification._authoritative_assertion(
            {"domain": "example.com", "domain_evidence": [bad]}) is None


def test_apply_adds_explicit_first_and_second_factors_idempotently(monkeypatch):
    cursor = Cursor([(1, "example.com", False, [authoritative()])])

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr(verification.company_db, "_cur", fake_cur)
    assert verification.apply_passes([passed()]) == {"selected": 1, "updated": 1}
    sql, params = cursor.calls[1]
    assert "domain_verified=TRUE" in sql
    assert verification.FIRST_PROVIDER in params
    evidence = json.loads(params[3])
    assert [(e["provider"], e["class"]) for e in evidence] == [
        (verification.FIRST_PROVIDER, "structured_corporate_source"),
        (verification.SECOND_PROVIDER, "official_site_identity")]
    assert all((e.get("candidate_domain") or e.get("domain")) == "example.com"
               for e in evidence)


def test_apply_rechecks_locked_domain_and_authoritative_factor(monkeypatch):
    cursor = Cursor([(1, "changed.com", False, [authoritative("changed.com")])])

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    monkeypatch.setattr(verification.company_db, "_cur", fake_cur)
    with pytest.raises(RuntimeError, match="domain changed"):
        verification.apply_passes([passed()])
    cursor.rows = [(1, "example.com", False, [])]
    with pytest.raises(RuntimeError, match="assertion missing"):
        verification.apply_passes([passed()])


def test_live_orchestration_never_calls_careers_and_only_applies_identity_pass(monkeypatch):
    rows = [
        {"id": 1, "legal_name": "Exact Corp", "brand_name": "Exact",
         "domain": "exact.com", "brand_identity": {},
         "domain_evidence": [authoritative("exact.com")]},
        {"id": 2, "legal_name": "Other Corp", "brand_name": "Other",
         "domain": "other.com", "brand_identity": {},
         "domain_evidence": [authoritative("other.com")]},
    ]
    cursor = Cursor(rows)

    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor

    class Response:
        def __init__(self, url, text):
            self.url, self.text = url, text

    responses = iter([Response("https://exact.com/", "<title>Exact Corp</title>"),
                      Response("https://other.com/", "<main>Other Corp</main>")])
    monkeypatch.setattr(verification.company_db, "_cur", fake_cur)
    monkeypatch.setattr(verification, "_get", lambda *_a, **_k: next(responses))
    applied = []
    monkeypatch.setattr(verification, "apply_passes",
                        lambda items: applied.extend(items) or {
                            "selected": len(items), "updated": len(items)})
    result = verification.verify_unverified_mandatory(client=object(), min_interval=0)
    assert [item["company_id"] for item in applied] == [1]
    assert (result["selected"], result["passed"], result["failed"]) == (2, 1, 1)
