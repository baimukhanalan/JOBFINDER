from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.tools import company_jobs_db as db


def remote_job(**overrides):
    row = {
        "company_id": 7,
        "source": " Greenhouse ",
        "source_board_id": "acme",
        "source_job_id": 123,
        "title": " Support   Agent ",
        "location_raw": "Remote — US",
        "locations": ["Remote — US", "Remote — US", "Toronto"],
        "remote_type": "fully remote",
        "salary_min": "$40,000",
        "salary_max": "50000",
        "currency": "usd",
        "description": "Full JD\n with spacing",
        "apply_url": "https://boards.example/jobs/123",
        "source_payload": {"id": 123, "raw": True},
        "provenance": {"endpoint": "api/jobs"},
    }
    row.update(overrides)
    return row


def test_prepare_job_normalizes_complete_remote_record():
    row = db.prepare_job(remote_job())
    assert row["source"] == "greenhouse"
    assert row["source_job_id"] == "123"
    assert row["title"] == "Support Agent"
    assert row["remote_type"] == "remote"
    assert row["salary_min"] == Decimal("40000")
    assert row["currency"] == "USD"
    assert row["locations"] == ["Remote — US", "Toronto"]
    assert row["source_payload"]["raw"] is True


@pytest.mark.parametrize("remote_type", [None, "hybrid", "on-site", "unknown"])
def test_rejects_jobs_not_confirmed_remote(remote_type):
    with pytest.raises(ValueError, match="only confirmed remote"):
        db.prepare_job(remote_job(remote_type=remote_type))


def test_content_hash_is_stable_and_ignores_observation_noise():
    first = remote_job(source_payload={"request_id": "one"}, provenance={"run": 1})
    second = remote_job(source_payload={"request_id": "two"}, provenance={"run": 2},
                        title="Support Agent")
    assert db.job_content_hash(first) == db.job_content_hash(second)
    assert not db.has_meaningful_change(db.job_content_hash(first), second)
    assert db.job_content_hash(first) != db.job_content_hash(
        remote_job(description="A genuinely changed JD"))


def test_ats_timestamp_formats_are_safe_for_timestamptz():
    assert db.normalize_timestamp(1710000000000) == datetime.fromtimestamp(
        1710000000, tz=timezone.utc)
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    assert db.normalize_timestamp("Posted 2 Days Ago", now=now) == datetime(
        2026, 8, 24, 12, tzinfo=timezone.utc)
    assert db.normalize_timestamp("provider-specific someday") is None


def test_question_normalization_preserves_options_and_raw_payload():
    question = {"id": "country", "label": " Country? ", "type": "select",
                "required": True, "options": ["US", "CA"], "custom": "raw"}
    normalized = db.normalize_question(question, 4)
    assert normalized["source_question_id"] == "country"
    assert normalized["position"] == 4
    assert normalized["label"] == "Country?"
    assert normalized["options"] == ["US", "CA"]
    assert normalized["source_payload"]["custom"] == "raw"


class FakeCursor:
    def __init__(self, fetches=None):
        self.calls = []
        self.fetches = list(fetches or [])
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if sql.startswith("UPDATE") or "UPDATE company_remote_jobs j" in sql:
            self.rowcount = 3

    def executemany(self, sql, values):
        values = list(values)
        self.calls.append((sql, values))
        self.rowcount = len(values)

    def fetchone(self):
        return self.fetches.pop(0)

    def fetchall(self):
        return self.fetches.pop(0)


def fake_cursor(monkeypatch, cursor):
    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor
    monkeypatch.setattr(db, "_cur", fake_cur)


def test_upsert_is_idempotent_and_does_not_duplicate_snapshot(monkeypatch):
    row = remote_job()
    digest = db.job_content_hash(row)
    cursor = FakeCursor(fetches=[{"id": 44, "content_hash": digest}, {"id": 44}])
    fake_cursor(monkeypatch, cursor)
    result = db.upsert_job(row)
    assert result == {"job_id": 44, "content_hash": digest, "snapshot_created": False}
    assert sum("company_remote_job_snapshots" in sql for sql, _ in cursor.calls) == 0


def test_upsert_creates_snapshot_for_new_or_changed_content(monkeypatch):
    cursor = FakeCursor(fetches=[{"id": 44, "content_hash": "old"}, {"id": 44}])
    fake_cursor(monkeypatch, cursor)
    result = db.upsert_job(remote_job())
    snapshot_calls = [(sql, args) for sql, args in cursor.calls
                      if "INSERT INTO company_remote_job_snapshots" in sql]
    assert result["snapshot_created"] is True
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][1][0] == 44


def test_failed_question_scrape_preserves_existing_questions(monkeypatch):
    cursor = FakeCursor()
    fake_cursor(monkeypatch, cursor)
    assert db.store_questions(44, [], scrape_succeeded=False, error="timeout") == 0
    sql = " ".join(call[0] for call in cursor.calls)
    assert "questions_status='failed'" in sql
    assert "company_remote_job_question_attempts" in sql
    assert "DELETE FROM company_remote_job_questions" not in sql


def test_successful_question_scrape_authoritatively_replaces_all(monkeypatch):
    cursor = FakeCursor()
    fake_cursor(monkeypatch, cursor)
    questions = [{"id": "email", "label": "Email"},
                 {"id": "eligible", "label": "Eligible?", "required": True}]
    assert db.store_questions(44, questions, scrape_succeeded=True) == 2
    assert "company_remote_job_question_attempts" in cursor.calls[0][0]
    assert any(call[0].startswith("DELETE FROM company_remote_job_questions")
               for call in cursor.calls)
    insert = next(call for call in cursor.calls
                  if call[0].startswith("INSERT INTO company_remote_job_questions"))
    assert len(insert[1]) == 2
    assert "questions_status='success'" in cursor.calls[-1][0]


def test_incomplete_or_failed_scan_never_closes_missing_jobs(monkeypatch):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("database was opened"))
    assert db.mark_missing_jobs_closed(company_id=7, source="greenhouse",
           source_board_id="acme", seen_source_job_ids=[], scan_succeeded=False,
           scan_complete=True) == 0
    assert db.mark_missing_jobs_closed(company_id=7, source="greenhouse",
           source_board_id="acme", seen_source_job_ids=[], scan_succeeded=True,
           scan_complete=False) == 0


def test_successful_complete_scan_closes_only_its_board(monkeypatch):
    cursor = FakeCursor()
    fake_cursor(monkeypatch, cursor)
    assert db.mark_missing_jobs_closed(
        company_id=7, source=" Greenhouse ", source_board_id="acme",
        seen_source_job_ids=["1", "2"], scan_succeeded=True, scan_complete=True) == 3
    sql, args = cursor.calls[0]
    assert "company_id=%s AND source=%s AND source_board_id=%s" in sql
    assert "NOT (source_job_id = ANY(%s))" in sql
    assert "INSERT INTO company_remote_job_snapshots" in sql
    assert "'status','closed'" in sql
    assert args == (7, "greenhouse", "acme", ["1", "2"])


def test_schema_links_company_and_contains_history_and_questions(monkeypatch):
    cursor = FakeCursor()
    fake_cursor(monkeypatch, cursor)
    db.ensure_schema()
    sql = " ".join(call[0] for call in cursor.calls)
    assert "REFERENCES company_discovery(id)" in sql
    assert "company_remote_job_snapshots" in sql
    assert "company_remote_job_questions" in sql
    assert "company_remote_job_question_attempts" in sql
    assert "source_payload JSONB" in sql


def test_collector_facing_upsert_signature_adds_company_and_scan(monkeypatch):
    cursor = FakeCursor(fetches=[None, {"id": 44}])
    fake_cursor(monkeypatch, cursor)
    result = db.upsert_job(7, remote_job(company_id=None), scan_id=9)
    assert result["job_id"] == 44
    insert_args = next(args for sql, args in cursor.calls
                       if sql.startswith("INSERT INTO company_remote_jobs"))
    assert insert_args[0] == 7
    assert insert_args[-1] == 9


def test_save_questions_requires_explicit_state(monkeypatch):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("database was opened"))
    assert db.save_questions(1, None, "not_attempted") == 0
    with pytest.raises(ValueError, match="invalid question scrape state"):
        db.save_questions(1, [], "maybe")


def test_begin_and_finish_scan_scope_closure(monkeypatch):
    first = FakeCursor(fetches=[{"id": 91}])
    fake_cursor(monkeypatch, first)
    assert db.begin_scan(7, "Greenhouse", "acme") == 91
    assert first.calls[0][1] == (7, "greenhouse", "acme")

    second = FakeCursor(fetches=[{
        "company_id": 7, "source": "greenhouse", "source_board_id": "acme"}])
    fake_cursor(monkeypatch, second)
    assert db.finish_scan(91, ["1", "2"], complete=True) == 3
    assert any("UPDATE company_remote_job_scans" in sql for sql, _ in second.calls)
    closure = next((sql, args) for sql, args in second.calls
                   if "WITH missing AS" in sql)
    assert closure[1] == (7, "greenhouse", "acme", ["1", "2"])


def test_finish_scan_failure_and_closure_share_one_transaction(monkeypatch):
    cursor = FakeCursor(fetches=[{
        "company_id": 7, "source": "greenhouse", "source_board_id": " raw ",
    }])
    fake_cursor(monkeypatch, cursor)
    assert db.finish_scan(91, ["1"], complete=False, error="detail failed") == 0
    assert not any("WITH missing AS" in sql for sql, _ in cursor.calls)
    update = next(args for sql, args in cursor.calls
                  if "UPDATE company_remote_job_scans" in sql)
    assert update[:2] == (False, False)


def test_target_selection_is_supported_and_oldest_scan_first(monkeypatch):
    cursor = FakeCursor(fetches=[[{"id": 4, "ats_slug": " RawSlug "}]])
    fake_cursor(monkeypatch, cursor)
    rows = db.list_company_targets("novel", 10, supported_ats=("lever", "workday"))
    assert rows[0]["ats_slug"] == " RawSlug "
    sql, args = cursor.calls[0]
    assert "lower(c.ats)=ANY(%s)" in sql
    assert "m.domain_verified" in sql
    assert "PARTITION BY lower(c.ats),c.ats_slug" in sql
    assert "m.monitoring_status IN ('qualified','monitoring')" not in sql
    assert "ORDER BY last_scanned_at ASC NULLS FIRST" in sql
    assert args == ("novel", ["lever", "workday"], 10)


def test_nonblocking_board_lock_is_held_until_atomic_finish(monkeypatch):
    class Connection:
        def __init__(self):
            self.fetches = [
                {"locked": True}, {"id": 501},
                {"company_id": 7, "source": "lever", "source_board_id": " Raw "},
            ]
            self.calls = []
            self.commits = 0

        def cursor(self, cursor_factory=None):
            connection = self

            class Cursor(FakeCursor):
                def execute(self, sql, args=None):
                    connection.calls.append((sql, args))
                    super().execute(sql, args)

                def fetchone(self):
                    return connection.fetches.pop(0)

                def close(self):
                    pass
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            pass

    class Pool:
        def __init__(self):
            self.connection = Connection()
            self.returned = []

        def getconn(self):
            return self.connection

        def putconn(self, connection):
            self.returned.append(connection)

    pool = Pool()
    monkeypatch.setattr(db, "_get_pool", lambda: pool)
    db._scan_sessions.clear()
    assert db.begin_locked_scan(7, "Lever", " Raw ") == 501
    assert pool.returned == []
    assert db.finish_scan(501, ["j1"], complete=True) == 3
    assert pool.returned == [pool.connection]
    sql = " ".join(call[0] for call in pool.connection.calls)
    assert "pg_try_advisory_lock" in sql
    assert "WITH missing AS" in sql
    assert "pg_advisory_unlock" in sql


def test_busy_board_lock_returns_connection_without_starting_scan(monkeypatch):
    class Cursor:
        def execute(self, sql, args=None):
            pass

        def fetchone(self):
            return {"locked": False}

        def close(self):
            pass

    class Connection:
        def cursor(self, cursor_factory=None):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    class Pool:
        connection = Connection()

        def __init__(self):
            self.returned = False

        def getconn(self):
            return self.connection

        def putconn(self, connection):
            self.returned = True

    pool = Pool()
    monkeypatch.setattr(db, "_get_pool", lambda: pool)
    with pytest.raises(db.BoardScanLocked):
        db.begin_locked_scan(7, "lever", "acme")
    assert pool.returned is True
