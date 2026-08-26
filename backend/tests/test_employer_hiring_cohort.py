"""Regression tests for the reservoir -> verified hiring boundary."""
from __future__ import annotations

import inspect
from contextlib import contextmanager

from backend.tools import employer_acceptance_audit, employer_hiring_cohort
from backend.tools import employer_master_db, employer_scoring


def ready_row(**overrides):
    row = {
        "company_id": 7,
        "identity_status": "verified",
        "domain_verified": True,
        "domain": "example.com",
        "domain_evidence": [
            {"class": "structured_corporate_source", "provider": "fdic_bankfind",
             "candidate_domain": "example.com"},
            {"class": "official_site_identity", "provider": "official_site_identity",
             "homepage_url": "https://www.example.com/"},
        ],
        "entity_risk_flags": [],
        "careers_url": "https://jobs.example.com/careers",
        "ats": "workday",
        "ats_slug": "examplecareers",
        "authoritative_complete_scans": 1,
        "active_authoritative_jobs": 2,
        "hiring_cohort_status": "reservoir_candidate",
    }
    row.update(overrides)
    return row


def test_verified_hiring_requires_every_contract_factor():
    decision = employer_hiring_cohort.evaluate_hiring_contract(ready_row())
    assert decision["status"] == "verified_hiring"
    assert decision["eligible"] is True
    assert decision["blockers"] == []
    assert decision["evidence"]["domain_factor_count"] == 2


def test_two_domain_records_are_not_two_independent_factor_classes():
    evidence = [
        {"class": "structured_corporate_source", "candidate_domain": "example.com",
         "provider": "sec"},
        {"class": "structured_corporate_source", "candidate_domain": "example.com",
         "provider": "fdic"},
    ]
    decision = employer_hiring_cohort.evaluate_hiring_contract(
        ready_row(domain_evidence=evidence))
    assert decision["status"] == "evidence_incomplete"
    assert decision["blockers"] == ["official_site_domain_factor"]


def test_official_site_factor_must_link_the_exact_verified_domain():
    evidence = ready_row()["domain_evidence"]
    evidence[1] = {**evidence[1], "homepage_url": "https://unrelated.example/"}
    decision = employer_hiring_cohort.evaluate_hiring_contract(
        ready_row(domain_evidence=evidence))
    assert "official_site_domain_factor" in decision["blockers"]


def test_careers_ats_scan_and_real_activity_are_independent_required_gates():
    cases = {
        "careers_url": {"careers_url": ""},
        "supported_ats": {"ats": "unknown"},
        "ats_slug": {"ats_slug": ""},
        "authoritative_scan_complete": {"authoritative_complete_scans": 0},
        "active_job_observed": {"active_authoritative_jobs": 0},
    }
    for blocker, overrides in cases.items():
        decision = employer_hiring_cohort.evaluate_hiring_contract(
            ready_row(**overrides))
        assert decision["status"] == "evidence_incomplete"
        assert blocker in decision["blockers"]


def test_quarantined_identity_can_never_be_verified_hiring():
    decision = employer_hiring_cohort.evaluate_hiring_contract(
        ready_row(identity_status="quarantined"))
    assert decision["status"] == "quarantined"
    assert decision["eligible"] is False


def test_refresh_is_dry_run_by_default_and_apply_is_explicit(monkeypatch):
    class Cursor:
        def __init__(self):
            self.calls = []
            self.rowcount = 1

        def execute(self, sql, params=None):
            self.calls.append((sql, params))

        def fetchall(self):
            return [ready_row()]

    cursor = Cursor()

    @contextmanager
    def fake_cur(*_args, **_kwargs):
        yield cursor

    monkeypatch.setattr(employer_hiring_cohort.company_db, "_cur", fake_cur)
    result = employer_hiring_cohort.refresh_hiring_cohort(limit=10)
    assert result["applied"] is False
    assert result["verified_hiring"] == 1
    assert len(cursor.calls) == 1
    assert "UPDATE" not in cursor.calls[0][0]

    cursor.calls.clear()
    applied = employer_hiring_cohort.refresh_hiring_cohort(limit=10, apply=True)
    assert applied["applied"] is True
    assert "FOR UPDATE OF m" in cursor.calls[0][0]
    assert "hiring_cohort_status" in cursor.calls[1][0]


def test_schema_promotion_scoring_and_acceptance_use_separate_cohort_status():
    schema = inspect.getsource(employer_master_db.ensure_schema)
    identity = inspect.getsource(employer_master_db.refresh_identity_qualification)
    scoring = inspect.getsource(employer_scoring.score_employers)
    acceptance = inspect.getsource(employer_acceptance_audit.run_audit)
    assert "hiring_cohort_status" in schema
    assert "THEN 'qualified'" not in identity
    assert scoring.count("hiring_cohort_status='verified_hiring'") >= 4
    assert "invalid_verified_hiring_cohort" in acceptance
    assert "unverified_qualified_employers" in acceptance


def test_authoritative_activity_sql_binds_scan_and_job_to_ats_slug():
    sql = employer_hiring_cohort._CONTRACT_SELECT
    assert "scan.scan_complete AND scan.scan_succeeded" in sql
    assert "scan.source_board_id" in sql
    assert "job.last_scan_id" in sql
    assert "job.status='active'" in sql
