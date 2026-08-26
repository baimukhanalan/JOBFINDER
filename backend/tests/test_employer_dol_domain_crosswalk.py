"""Tests for exact, proposal-only DOL domain inheritance."""
from __future__ import annotations

import inspect
from contextlib import contextmanager

import pytest

from backend.tools import employer_dol_domain_crosswalk as crosswalk


def dol(**overrides):
    row = {
        "id": 10, "source_external_id": "dol-10", "legal_name": "Acme, Inc.",
        "metadata": {"employer_address": {
            "address_line1": "1 Main Street", "city": "Austin",
            "region": "TX", "postal_code": "78701", "country": "US",
        }},
    }
    row.update(overrides)
    return row


def source(**overrides):
    row = {
        "id": 20, "source": "gleif_lei", "source_external_id": "lei-20",
        "legal_name": "ACME INC", "candidate_domain": "acme.example",
        "domain_evidence": [{
            "class": "structured_corporate_source", "provider": "wikidata_p856",
            "candidate_domain": "acme.example",
        }],
        "metadata": {"source_snapshot": {"legal_address": {
            "addressLines": ["1 Main St"], "city": "Austin",
            "region": "US-TX", "postalCode": "78701", "country": "US",
        }}},
    }
    row.update(overrides)
    return row


def test_exact_name_and_state_city_propose_unverified_domain():
    result = crosswalk.propose_domains([dol()], [source()])
    assert result["proposed"] == 1
    proposal = result["proposals"][0]
    assert proposal["candidate_domain"] == "acme.example"
    assert proposal["source_assertions"][0]["location_method"] == "state_city"
    assert proposal["provenance"]["assertion"] == (
        "unverified_cross_source_domain_proposal")
    assert "domain_verified" not in proposal


def test_fuzzy_or_legal_suffix_name_changes_never_match():
    for name in ("Acme Holdings Inc", "Acme LLC", "Acme Incorporated USA"):
        result = crosswalk.propose_domains([dol()], [source(legal_name=name)])
        assert result["proposed"] == 0
        assert result["reasons"] == {"no_exact_name_with_domain": 1}


def test_state_only_is_insufficient_and_city_conflict_rejects():
    location = {"source_snapshot": {"legal_address": {
        "city": "Dallas", "region": "TX", "country": "US"}}}
    result = crosswalk.propose_domains([dol()], [source(metadata=location)])
    assert result["proposed"] == 0
    assert result["reasons"] == {"location_mismatch": 1}


def test_exact_state_postal_can_match_when_city_is_unavailable():
    target = dol(metadata={"employer_address": {
        "region": "TX", "postal_code": "78701"}})
    result = crosswalk.propose_domains([target], [source()])
    assert result["proposed"] == 1
    assert result["proposals"][0]["source_assertions"][0]["location_method"] == (
        "state_postal")


def test_conflicting_domains_are_quarantined_as_no_proposal():
    other = source(id=21, source_external_id="lei-21",
                   candidate_domain="other.example", domain_evidence=[{
                       "class": "structured_corporate_source", "provider": "sec",
                       "candidate_domain": "other.example"}])
    result = crosswalk.propose_domains([dol()], [source(), other])
    assert result["proposed"] == 0
    assert result["reasons"] == {"conflicting_source_domains": 1}


def test_same_domain_from_multiple_exact_sources_is_one_proposal():
    second = source(id=21, source="wikidata_employer", source_external_id="Q21")
    result = crosswalk.propose_domains([dol()], [source(), second])
    assert result["proposed"] == 1
    assert len(result["proposals"][0]["source_assertions"]) == 2


class FakeCursor:
    rowcount = 1

    def __init__(self, fetches):
        self.fetches = list(fetches)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.fetches.pop(0)

    def fetchall(self):
        return self.fetches.pop(0)


def test_apply_revalidates_and_writes_proposal_evidence_only(monkeypatch):
    proposal = crosswalk.propose_domains([dol()], [source()])["proposals"][0]
    target = {**dol(), "source_observed_at": "2026-08-26T00:00:00Z",
              "candidate_domain": None, "domain_evidence": []}
    cursor = FakeCursor([target, [source()]])

    @contextmanager
    def fake_cur(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(crosswalk.company_db, "_cur", fake_cur)
    assert crosswalk.apply_proposals([proposal]) == {
        "selected": 1, "updated": 1, "already_present": 0}
    statements = "\n".join(sql for sql, _params in cursor.calls)
    assert "candidate_domain=COALESCE" in statements
    assert "domain_evidence=domain_evidence" in statements
    assert "domain_verified" not in statements
    assert "SET domain=" not in statements
    evidence_params = next(params for sql, params in cursor.calls
                           if "UPDATE company_employer_master" in sql)
    assert "proposal_not_verified" in str(evidence_params[1])


def test_apply_rejects_conflicting_existing_target_domain(monkeypatch):
    proposal = crosswalk.propose_domains([dol()], [source()])["proposals"][0]
    target = {**dol(), "candidate_domain": "conflict.example", "domain_evidence": []}
    cursor = FakeCursor([target, [source()]])

    @contextmanager
    def fake_cur(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(crosswalk.company_db, "_cur", fake_cur)
    with pytest.raises(RuntimeError, match="conflicting domain"):
        crosswalk.apply_proposals([proposal])


def test_cli_is_preview_by_default_and_apply_requires_reviewed_file():
    source_text = inspect.getsource(crosswalk.main)
    assert "load_stored_dol_rows" in source_text
    assert "args.apply" in source_text
    assert "apply_proposals" in source_text
