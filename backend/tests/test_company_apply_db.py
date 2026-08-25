from contextlib import contextmanager

import pytest

from backend.tools import company_apply_db as db


class FakeCursor:
    def __init__(self, fetches=None, rows=None, rowcounts=None):
        self.calls = []
        self.fetches = list(fetches or [])
        self.rows = list(rows or [])
        self.rowcounts = list(rowcounts or [])
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0

    def fetchone(self):
        return self.fetches.pop(0) if self.fetches else None

    def fetchall(self):
        return self.rows


def fake_cursor(monkeypatch, cursor):
    @contextmanager
    def fake_cur(dict_rows=True):
        yield cursor
    monkeypatch.setattr(db, "_cur", fake_cur)


def eligible_row(**overrides):
    row = {
        "id": 3, "job_id": 10, "profile_id": "p1", "state": "queued",
        "revalidation_hash": "same", "current_revalidation_hash": "same",
        "job_status": "active", "remote_type": "remote",
        "questions_status": "success", "questions": [{"label": "Name"}],
    }
    row.update(overrides)
    return row


def test_automatic_submission_requires_dedicated_authorized_states():
    assert "submitted" not in db.STATES
    assert "human_submitted" in db.STATES
    assert "submit_approved" in db.STATES
    assert "submitting" in db.STATES
    assert "auto_submitted" in db.STATES
    assert db.TRANSITIONS["ready_for_review"] == {
        "human_submitted", "needs_input", "blocked"}
    assert db.TRANSITIONS["awaiting_approval"] >= {"submit_approved"}
    assert db.TRANSITIONS["submitting"] >= {"auto_submitted", "submission_failed"}


def test_url_hash_canonicalizes_fragment_case_and_trailing_slash():
    assert db.apply_url_hash(" HTTPS://EXAMPLE.COM/apply/#x ") == \
        db.apply_url_hash("https://example.com/apply")


def test_schema_is_isolated_and_has_uniqueness_leases_and_audit(monkeypatch):
    cursor = FakeCursor()
    fake_cursor(monkeypatch, cursor)
    db.ensure_schema()
    sql = " ".join(call[0] for call in cursor.calls)
    assert "REFERENCES company_remote_jobs(id)" in sql
    assert "UNIQUE (job_id, profile_id)" in sql
    assert "UNIQUE (profile_id, apply_url_hash)" in sql
    assert "company_remote_application_attempts" in sql
    assert "company_remote_application_reviews" in sql
    assert "company_remote_application_profile_leases" in sql
    assert "human_submitted" in sql
    assert "company_remote_application_batches" in sql


def test_enqueue_enforces_all_safety_gates_and_catalog_exclusion(monkeypatch):
    cursor = FakeCursor(rowcounts=[4])
    fake_cursor(monkeypatch, cursor)
    assert db.enqueue_eligible("person", freshness_days=3, limit=20) == 4
    sql, args = cursor.calls[0]
    assert "j.status='active'" in sql
    assert "j.remote_type='remote'" in sql
    assert "j.questions_status='success'" in sql
    assert "j.apply_url ~* '^https://'" in sql
    assert "j.last_seen_at >=" in sql
    assert "NOT EXISTS" in sql and "FROM job_catalog old" in sql
    assert "old.external_id=j.source_job_id" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert args == ("person", 3, 20)


@pytest.mark.parametrize("days,limit", [(0, 1), (1, 0)])
def test_enqueue_rejects_unbounded_or_invalid_limits(monkeypatch, days, limit):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("opened database"))
    with pytest.raises(ValueError):
        db.enqueue_eligible("p", freshness_days=days, limit=limit)


def test_claim_uses_profile_lease_and_skip_locked_and_returns_joined_data(monkeypatch):
    row = eligible_row(company_name="Acme")
    cursor = FakeCursor(fetches=[{"profile_id": "p1"}, row,
                                 {"id": 3, "state": "claimed"}])
    fake_cursor(monkeypatch, cursor)
    result = db.claim_next("p1", "worker", lease_seconds=300)
    sql = " ".join(call[0] for call in cursor.calls)
    assert "company_remote_application_profile_leases" in sql
    assert "FOR UPDATE OF a SKIP LOCKED" in sql
    assert "company_remote_job_questions" in sql
    assert "current_revalidation_hash" in sql
    assert result["state"] == "claimed"
    assert result["company_name"] == "Acme"


def test_approved_claim_enters_preparing_not_submitted(monkeypatch):
    row = eligible_row(state="approved")
    cursor = FakeCursor(fetches=[{"profile_id": "p1"}, row,
                                 {"id": 3, "state": "preparing"}])
    fake_cursor(monkeypatch, cursor)
    result = db.claim_next("p1", "worker", from_states=("approved",))
    update = next((sql, args) for sql, args in cursor.calls
                  if "UPDATE company_remote_applications SET state=" in sql)
    assert update[1][0] == "preparing"
    assert result["state"] == "preparing"


def test_submit_authorized_claim_enters_submitting(monkeypatch):
    row = eligible_row(state="submit_approved")
    cursor = FakeCursor(fetches=[{"profile_id": "p1"}, row,
                                 {"id": 3, "state": "submitting"}])
    fake_cursor(monkeypatch, cursor)
    result = db.claim_next("p1", "worker", from_states=("submit_approved",))
    update = next((sql, args) for sql, args in cursor.calls
                  if "UPDATE company_remote_applications SET state=" in sql)
    assert update[1][0] == "submitting"
    assert result["state"] == "submitting"


def test_submit_claim_can_be_scoped_to_exact_authorization_batch(monkeypatch):
    row = eligible_row(state="submit_approved", submission_batch_id="batch-123")
    cursor = FakeCursor(fetches=[{"profile_id": "p1"}, row,
                                 {"id": 3, "state": "submitting"}])
    fake_cursor(monkeypatch, cursor)

    result = db.claim_next(
        "p1", "worker", from_states=("submit_approved",),
        submission_batch_id="batch-123",
    )

    select_sql, select_args = next(
        (sql, args) for sql, args in cursor.calls if "FOR UPDATE OF a SKIP LOCKED" in sql)
    assert "a.submission_batch_id=%s" in select_sql
    assert select_args[-1] == "batch-123"
    assert result["submission_batch_id"] == "batch-123"


def test_batch_scope_is_rejected_for_non_submission_claim(monkeypatch):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("opened database"))
    with pytest.raises(ValueError, match="submit_approved"):
        db.claim_next("p1", "worker", from_states=("queued",),
                      submission_batch_id="batch-123")


def test_busy_profile_returns_none_without_claiming(monkeypatch):
    cursor = FakeCursor(fetches=[None])
    fake_cursor(monkeypatch, cursor)
    assert db.claim_next("p1", "other") is None
    assert not any("FOR UPDATE OF a SKIP LOCKED" in sql for sql, _ in cursor.calls)


def test_claim_rejects_illegal_source_or_target_before_database(monkeypatch):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("opened database"))
    with pytest.raises(ValueError):
        db.claim_next("p", "w", from_states=("human_submitted",))
    with pytest.raises(db.ApplicationStateError):
        db.claim_next("p", "w", from_states=("approved",), claimed_state="claimed")


def test_recovery_never_loses_prior_human_approval(monkeypatch):
    cursor = FakeCursor(rowcounts=[2, 2])
    fake_cursor(monkeypatch, cursor)
    assert db.recover_stale_leases() == 2
    sql = cursor.calls[0][0]
    assert "WHEN state='preparing' THEN 'approved'" in sql
    assert "WHEN state='submitting' THEN 'submission_failed'" in sql
    assert "state IN ('claimed','preparing','submitting')" in sql


def test_renew_requires_matching_live_profile_lease(monkeypatch):
    cursor = FakeCursor(rowcounts=[1, 1])
    fake_cursor(monkeypatch, cursor)
    assert db.renew_lease(3, "p1", "worker", lease_seconds=120)
    assert "p.application_id=%s" in cursor.calls[0][0]
    assert "a.state IN ('claimed','preparing','submitting')" in cursor.calls[0][0]


def test_transition_rejects_changed_job_or_questions(monkeypatch):
    cursor = FakeCursor(fetches=[eligible_row(state="claimed",
                                               current_revalidation_hash="changed")])
    fake_cursor(monkeypatch, cursor)
    with pytest.raises(db.StaleApplicationError, match="questions changed"):
        db.transition(3, "awaiting_approval", "worker",
                      expected_revalidation_hash="same")
    assert not any("SET state=" in sql for sql, _ in cursor.calls)


def test_transition_checks_state_machine_and_releases_profile(monkeypatch):
    current = eligible_row(state="claimed")
    updated = dict(current, state="awaiting_approval", claimed_by=None)
    cursor = FakeCursor(fetches=[current, updated])
    fake_cursor(monkeypatch, cursor)
    result = db.transition(3, "awaiting_approval", "worker",
                           expected_revalidation_hash="same")
    assert result["state"] == "awaiting_approval"
    assert any("state_transition" in sql for sql, _ in cursor.calls)
    assert any(sql.startswith("DELETE FROM company_remote_application_profile_leases")
               for sql, _ in cursor.calls)


def test_transition_persists_current_worker_artifacts(monkeypatch):
    current = eligible_row(state="claimed")
    updated = dict(current, state="awaiting_approval", artifact_dir="/safe/run/3",
                   fit_score=88)
    cursor = FakeCursor(fetches=[current, updated])
    fake_cursor(monkeypatch, cursor)
    result = db.transition(3, "awaiting_approval", "worker", payload={
        "artifact_dir": "/safe/run/3", "report": {"complete": True},
        "policy_result": {"allowed": True}, "fit_score": 88,
    })
    update_args = next(args for sql, args in cursor.calls
                       if "artifact_dir=COALESCE" in sql)
    assert update_args[3] == "/safe/run/3"
    assert update_args[7] == 88
    assert result["fit_score"] == 88


def test_cannot_skip_human_review_to_submission(monkeypatch):
    cursor = FakeCursor(fetches=[eligible_row(state="preparing")])
    fake_cursor(monkeypatch, cursor)
    with pytest.raises(db.ApplicationStateError):
        db.transition(3, "human_submitted", "worker")


def test_auto_submitted_requires_positive_confirmation(monkeypatch):
    cursor = FakeCursor(fetches=[eligible_row(state="submitting")])
    fake_cursor(monkeypatch, cursor)
    with pytest.raises(db.ApplicationStateError, match="mark_auto_submitted"):
        db.transition(3, "auto_submitted", "worker", payload={"receipt": {
            "confirmed": False, "clicked": True}})
    with pytest.raises(ValueError, match="positive confirmation"):
        db.mark_auto_submitted(3, "worker", receipt={"confirmed": False},
                               expected_revalidation_hash="same")


def test_generic_transition_cannot_bypass_batch_authorization(monkeypatch):
    cursor = FakeCursor(fetches=[eligible_row(state="awaiting_approval")])
    fake_cursor(monkeypatch, cursor)
    with pytest.raises(db.ApplicationStateError, match="authorize_batch"):
        db.transition(3, "submit_approved", "worker")


def test_batch_authorization_is_count_bound_hash_bound_and_audited(monkeypatch):
    row = eligible_row(state="awaiting_approval")
    cursor = FakeCursor(rows=[row])
    fake_cursor(monkeypatch, cursor)

    result = db.authorize_batch("p1", [3], "alan", "SEND 1", {3: "same"})

    assert result["count"] == 1
    sql = " ".join(call[0] for call in cursor.calls)
    assert "FOR UPDATE OF a" in sql
    assert "state='submit_approved'" in sql
    assert "authorize_auto_submit" in sql
    assert "batch_authorization" in sql


def test_batch_authorization_rejects_wrong_confirmation_before_database(monkeypatch):
    monkeypatch.setattr(db, "_cur", lambda *_: pytest.fail("opened database"))
    with pytest.raises(ValueError, match="SEND 2"):
        db.authorize_batch("p1", [1, 2], "alan", "SEND 1", {1: "a", 2: "b"})


def test_approve_reject_and_human_submission_are_audited(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "transition", lambda *a, **kw:
                        calls.append((a, kw)) or {"id": a[0], "state": a[1]})
    assert db.approve(1, "alan", "hash")["state"] == "approved"
    assert db.reject(2, "alan", "not a fit")["state"] == "rejected"
    assert [item[1]["_review_action"] for item in calls] == ["approve", "reject"]

    assert db.mark_human_submitted(3, "alan", receipt={"confirmation": "abc"})[
        "state"] == "human_submitted"
    assert calls[-1][1]["_review_action"] == "human_submitted"


def test_human_submission_columns_and_audit_are_same_transaction(monkeypatch):
    current = eligible_row(state="ready_for_review")
    updated = dict(current, state="human_submitted", human_submitted_by="alan")
    cursor = FakeCursor(fetches=[current, updated])
    fake_cursor(monkeypatch, cursor)
    result = db.mark_human_submitted(3, "alan", receipt={"confirmation": "abc"})
    assert result["state"] == "human_submitted"
    sql = " ".join(call[0] for call in cursor.calls)
    assert "human_submitted_at=CASE" in sql
    assert "company_remote_application_reviews" in sql


def test_get_and_list_return_job_company_questions_and_hash(monkeypatch):
    row = eligible_row(company_name="Acme")
    one = FakeCursor(fetches=[row])
    fake_cursor(monkeypatch, one)
    assert db.get_application(3)["company_name"] == "Acme"
    assert "company_remote_job_questions" in one.calls[0][0]
    assert "current_revalidation_hash" in one.calls[0][0]

    many = FakeCursor(rows=[row])
    fake_cursor(monkeypatch, many)
    result = db.list_applications("p1", "queued", limit=5)
    assert result[0]["questions"]
    assert many.calls[0][1] == ("p1", "queued", 5)


def test_stats_can_scope_profile(monkeypatch):
    cursor = FakeCursor(rows=[{"state": "queued", "count": 2},
                              {"state": "approved", "count": 1}])
    fake_cursor(monkeypatch, cursor)
    assert db.stats("p1") == {"total": 3, "by_state": {"queued": 2, "approved": 1}}
    assert cursor.calls[0][1] == ("p1",)
