"""Append-only job observation/history regression tests."""
from __future__ import annotations

import inspect
import re
from contextlib import contextmanager

import pytest

from backend.tools import company_jobs as collector
from backend.tools import company_jobs_db as db
from backend.tools.company_job_sources import JobFetchResult


def _job(**overrides):
    row = {
        "company_id": 7, "source": "workday", "source_board_id": "DXCJobs",
        "source_job_id": "51587362", "title": "Mainframe/ITOM",
        "remote_type": "remote", "is_remote": True,
        "salary_min": "90000", "salary_max": "120000", "currency": "USD",
        "description": "Original complete JD", "status": "active",
        "apply_url": "https://example.test/job/51587362",
        "source_payload": {"id": "51587362"},
        "provenance": {"endpoint": "workday_cxs"},
    }
    row.update(overrides)
    return row


class Cursor:
    def __init__(self, fetches=()):
        self.fetches = list(fetches)
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if "UPDATE company_remote_jobs j" in sql:
            self.rowcount = 1

    def fetchone(self):
        return self.fetches.pop(0)


def _use_cursor(monkeypatch, cursor):
    @contextmanager
    def fake_cur(_dict_rows=True):
        yield cursor
    monkeypatch.setattr(db, "_cur", fake_cur)


@pytest.mark.parametrize("change", [
    {"description": "Changed complete JD"},
    {"salary_min": "95000"},
    {"salary_max": "130000"},
    {"compensation_text": "$95k-$130k"},
    {"status": "closed"},
])
def test_jd_salary_and_status_are_meaningful_history_changes(change):
    original = _job()
    assert db.job_content_hash(original) != db.job_content_hash(_job(**change))


@pytest.mark.parametrize(("previous", "status", "event"), [
    (None, "active", "first_seen"),
    ({"status": "active"}, "active", "content_changed"),
    ({"status": "active"}, "closed", "closed"),
    ({"status": "closed"}, "active", "reopened"),
])
def test_snapshot_transition_classification(previous, status, event):
    assert db.snapshot_event(previous, status) == event


def test_reopen_appends_event_preserves_first_seen_and_clears_closed_at(monkeypatch):
    cursor = Cursor(fetches=[
        {"id": 4, "content_hash": "closed-hash", "status": "closed"}, {"id": 4},
    ])
    _use_cursor(monkeypatch, cursor)

    result = db.upsert_job(_job(status="active"), scan_id=22)

    assert result["snapshot_created"] is True
    upsert_sql = next(sql for sql, _ in cursor.calls
                      if sql.startswith("INSERT INTO company_remote_jobs"))
    update_clause = upsert_sql.split("DO UPDATE SET", 1)[1]
    assert "first_seen_at" not in update_clause
    assert "last_seen_at=now()" in update_clause
    assert "ELSE NULL END" in update_clause
    snapshot_args = next(args for sql, args in cursor.calls
                         if "INSERT INTO company_remote_job_snapshots" in sql)
    assert snapshot_args[0:2] == (4, "reopened")


def test_explicit_closed_observation_sets_closed_at_and_closed_event(monkeypatch):
    cursor = Cursor(fetches=[
        {"id": 4, "content_hash": db.job_content_hash(_job()), "status": "active"},
        {"id": 4},
    ])
    _use_cursor(monkeypatch, cursor)
    db.upsert_job(_job(status="closed"), scan_id=23)
    snapshot_args = next(args for sql, args in cursor.calls
                         if "INSERT INTO company_remote_job_snapshots" in sql)
    assert snapshot_args[1] == "closed"
    upsert_sql = next(sql for sql, _ in cursor.calls
                      if sql.startswith("INSERT INTO company_remote_jobs"))
    assert "THEN COALESCE(company_remote_jobs.closed_at,now())" in upsert_sql


def test_complete_scan_closure_snapshot_contains_full_content_and_event(monkeypatch):
    cursor = Cursor()
    _use_cursor(monkeypatch, cursor)
    assert db.mark_missing_jobs_closed(
        company_id=7, source="workday", source_board_id="DXCJobs",
        seen_source_job_ids=["51587362"], scan_succeeded=True,
        scan_complete=True) == 1
    sql, _args = cursor.calls[0]
    assert "(job_id,event_type,content_hash" in sql
    assert "SELECT id,'closed'" in sql
    for field in db._MEANINGFUL_FIELDS:
        assert f"'{field}'" in sql


def test_scan_finalization_is_exactly_once(monkeypatch):
    cursor = Cursor(fetches=[{
        "company_id": 7, "source": "workday", "source_board_id": "DXCJobs",
        "finished_at": "2026-08-26T10:00:00Z",
    }])
    _use_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError, match="already finalized"):
        db.finish_scan(91, ["51587362"], complete=True)
    sql = " ".join(statement for statement, _ in cursor.calls)
    assert "UPDATE company_remote_job_scans" not in sql
    assert "WITH missing AS" not in sql


def test_snapshot_store_is_append_only_and_no_legacy_catalog_is_mutated():
    source = inspect.getsource(db)
    assert "UPDATE company_remote_job_snapshots" not in source
    assert "DELETE FROM company_remote_job_snapshots" not in source
    mutations = re.findall(
        r"(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)", source, re.I)
    assert mutations
    # ``SET`` is the regex tail of ``DO UPDATE SET`` in the dynamically assembled
    # upsert, not a table name.
    assert all(table == "SET" or table.startswith("company_remote_")
               for table in mutations)
    assert not any(table in {"job_catalog", "targets", "vacancies"}
                   for table in mutations)


class CycleStore:
    BoardScanLocked = db.BoardScanLocked

    def __init__(self):
        self.finished = []
        self.saved = []

    def get_company_target(self, company_id, **_kwargs):
        return {"id": company_id, "canonical_name": "DXC", "ats": "workday",
                "ats_slug": "DXCJobs", "ats_url": "https://example.test/DXCJobs"}

    def begin_scan(self, *_args):
        return len(self.finished) + 1

    def upsert_job(self, company_id, row, scan_id):
        self.saved.append((company_id, row, scan_id))
        return {"job_id": 4, "snapshot_created": False}

    def finish_scan(self, scan_id, seen, complete=True, error=None):
        self.finished.append((scan_id, seen, complete, error))
        return 0

    def save_questions(self, *_args, **_kwargs):
        return 0


def test_incomplete_cycle_keeps_observations_but_never_requests_closure():
    store = CycleStore()
    result = collector.collect_company_jobs(
        company_id=7, store=store, collect_questions=False,
        fetcher=lambda *_args, **_kwargs: JobFetchResult(
            [_job()], complete=False, errors=["page 2 timeout"]),
    )
    assert result["jobs_stored"] == 1
    assert result["companies_incomplete"] == 1
    assert store.finished == [(1, ["51587362"], False, "page 2 timeout")]
