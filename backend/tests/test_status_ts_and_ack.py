"""Timestamped status writes (status_store "ts") and ack->submitted auto-record
(inbox_index._auto_advance): a confirmation email marks an untracked submit,
and never downgrades an already-advanced status.
"""
import json
from datetime import datetime

import pytest

import backend.status_store as ss
import backend.inbox_index as ii


@pytest.fixture
def status_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_status_path",
                        lambda profile: tmp_path / profile / "status.json")
    return tmp_path


def _read(status_root, profile="kate"):
    return json.loads((status_root / profile / "status.json").read_text())


# ---------------------------------------------------------------------------
# ts stamping
# ---------------------------------------------------------------------------

def test_mark_stamps_ts_utc(status_root):
    ss.mark("kate", "job-1", "submitted")
    entry = _read(status_root)["job-1"]
    assert entry["status"] == "submitted"
    ts = datetime.fromisoformat(entry["ts"])  # must parse as ISO-8601
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0  # UTC, not local time


def test_mark_refreshes_ts_on_rewrite(status_root, monkeypatch):
    ss.mark("kate", "job-1", "pending")
    first = _read(status_root)["job-1"]["ts"]
    monkeypatch.setattr(ss, "_now_iso", lambda: "2099-01-01T00:00:00+00:00")
    ss.mark("kate", "job-1", "submitted")
    entry = _read(status_root)["job-1"]
    assert entry["ts"] == "2099-01-01T00:00:00+00:00"
    assert entry["ts"] != first


def test_entries_without_ts_stay_readable(status_root):
    # status.json written before ts existed: bare {"status": ...} entries.
    sf = status_root / "kate" / "status.json"
    sf.parent.mkdir(parents=True)
    sf.write_text(json.dumps({"old-job": {"status": "interview"}}))
    assert ss.load("kate") == {"old-job": {"status": "interview"}}
    ss.mark("kate", "new-job", "submitted")
    data = _read(status_root)
    # untouched legacy entry is preserved verbatim (no ts retrofitted)
    assert data["old-job"] == {"status": "interview"}
    assert data["new-job"]["status"] == "submitted"
    assert "ts" in data["new-job"]


def test_mark_empty_still_removes(status_root):
    ss.mark("kate", "job-1", "submitted")
    ss.mark("kate", "job-1", "")  # undo
    assert "job-1" not in _read(status_root)


# ---------------------------------------------------------------------------
# ack -> submitted via _auto_advance
# ---------------------------------------------------------------------------

ACK_MSG = {"from": "no-reply@acmecorp.com", "subject": "We received your application",
           "category": "ack"}
INTERVIEW_MSG = {"from": "hr@acmecorp.com", "subject": "Schedule a call",
                 "category": "interview"}


@pytest.fixture
def acme_tokens(monkeypatch):
    monkeypatch.setattr(ii, "_build_company_tokens",
                        lambda pid: {"acmecorp": "job-1"})


def test_ack_marks_untracked_submit(status_root, acme_tokens):
    ii._auto_advance("kate", [ACK_MSG])
    entry = _read(status_root)["job-1"]
    assert entry["status"] == "submitted"
    assert "ts" in entry


def test_ack_over_pending_marks_submit(status_root, acme_tokens):
    ss.mark("kate", "job-1", "pending")
    ii._auto_advance("kate", [ACK_MSG])
    assert _read(status_root)["job-1"]["status"] == "submitted"


@pytest.mark.parametrize("terminal", ["submitted", "interview", "rejected"])
def test_ack_never_downgrades(status_root, acme_tokens, terminal):
    ss.mark("kate", "job-1", terminal)
    before = _read(status_root)["job-1"]
    ii._auto_advance("kate", [ACK_MSG])
    # status untouched, ts not refreshed (no write happened)
    assert _read(status_root)["job-1"] == before


def test_ack_after_interview_same_run(status_root, acme_tokens):
    # interview email advances first; the ack later in the same batch must not
    # demote it back to submitted
    ii._auto_advance("kate", [INTERVIEW_MSG, ACK_MSG])
    assert _read(status_root)["job-1"]["status"] == "interview"


def test_ack_unmatched_company_is_ignored(status_root, acme_tokens):
    ii._auto_advance("kate", [{"from": "jobs@othercorp.com",
                               "subject": "Thanks for applying",
                               "category": "ack"}])
    assert not (status_root / "kate" / "status.json").exists()


def test_interview_alert_kinds_unchanged(status_root, acme_tokens):
    # interview/assessment/rejection advancing kept intact alongside ack
    ii._auto_advance("kate", [{"from": "hr@acmecorp.com",
                               "subject": "Online assessment invitation",
                               "category": "assessment"}])
    assert _read(status_root)["job-1"]["status"] == "interview"
