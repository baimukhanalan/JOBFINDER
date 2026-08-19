"""Tests for N1-N3: notify(), advance_decision, match_company, new_messages,
status_store.mark, and the refactored _apply_mark / _mark_submitted wrappers.
"""
import json
import os

import pytest

# ---------------------------------------------------------------------------
# status_store.mark
# ---------------------------------------------------------------------------

def test_status_store_mark_creates_file(tmp_path, monkeypatch):
    import backend.status_store as ss
    monkeypatch.setattr(ss, "_status_path",
                        lambda profile: tmp_path / profile / "status.json")
    ss.mark("alice", "job-1", "submitted")
    f = tmp_path / "alice" / "status.json"
    assert f.exists()
    data = json.loads(f.read_text())
    assert data["job-1"]["status"] == "submitted"
    assert "ts" in data["job-1"]  # every write is timestamped
    # no .tmp left behind
    assert not list((tmp_path / "alice").glob("*.tmp"))


def test_status_store_mark_merges(tmp_path, monkeypatch):
    import backend.status_store as ss
    monkeypatch.setattr(ss, "_status_path",
                        lambda profile: tmp_path / profile / "status.json")
    ss.mark("alice", "job-1", "submitted")
    ss.mark("alice", "job-2", "interview")
    data = json.loads((tmp_path / "alice" / "status.json").read_text())
    assert data["job-1"]["status"] == "submitted"
    assert data["job-2"]["status"] == "interview"


def test_status_store_mark_empty_removes(tmp_path, monkeypatch):
    import backend.status_store as ss
    monkeypatch.setattr(ss, "_status_path",
                        lambda profile: tmp_path / profile / "status.json")
    ss.mark("alice", "job-1", "submitted")
    ss.mark("alice", "job-1", "")  # undo
    data = json.loads((tmp_path / "alice" / "status.json").read_text())
    assert "job-1" not in data


# ---------------------------------------------------------------------------
# advance_decision
# ---------------------------------------------------------------------------

from backend.inbox_index import advance_decision


def test_advance_interview_over_pending():
    assert advance_decision("interview", "") == "interview"
    assert advance_decision("interview", "pending") == "interview"
    assert advance_decision("interview", "submitted") == "interview"


def test_advance_assessment_over_pending():
    assert advance_decision("assessment", "") == "interview"
    assert advance_decision("assessment", "submitted") == "interview"


def test_advance_interview_already_interview():
    assert advance_decision("interview", "interview") is None


def test_advance_interview_over_rejected():
    # interview after rejection: don't move backwards (user rejected, then got email)
    assert advance_decision("interview", "rejected") is None


def test_advance_rejection_over_submitted():
    assert advance_decision("rejection", "submitted") == "rejected"


def test_advance_rejection_over_interview():
    # a rejection after interview is valid — company decided not to proceed
    assert advance_decision("rejection", "interview") == "rejected"


def test_advance_rejection_already_rejected():
    assert advance_decision("rejection", "rejected") is None


def test_advance_ack_records_submit():
    # a confirmation email is evidence the submit happened without being recorded
    assert advance_decision("ack", "") == "submitted"
    assert advance_decision("ack", "pending") == "submitted"


def test_advance_ack_never_downgrades():
    assert advance_decision("ack", "submitted") is None
    assert advance_decision("ack", "interview") is None
    assert advance_decision("ack", "rejected") is None


def test_advance_other_is_none():
    assert advance_decision("other", "") is None


# ---------------------------------------------------------------------------
# match_company
# ---------------------------------------------------------------------------

from backend.inbox_index import match_company

TOKENS = {
    "greenhouse": "job-gh",
    "acme": "job-acme",
    "zapier": "job-zapier",
    "ab": "job-short",   # too short (< 4 chars)
}


def test_match_by_sender_domain():
    assert match_company("hr@jobs.greenhouse.io", "You applied", TOKENS) == "job-gh"


def test_match_by_subject():
    assert match_company("noreply@example.com", "Update from Acme Corporation", TOKENS) == "job-acme"


def test_no_match_zero():
    assert match_company("hr@nowhere.com", "Random subject", TOKENS) is None


def test_no_match_multiple():
    # Subject contains both greenhouse and acme — ambiguous, must return None
    assert match_company("hr@greenhouse.io", "Acme via greenhouse", TOKENS) is None


def test_short_token_skipped():
    # "ab" is in tokens but len < 4 — must never match
    assert match_company("hr@ab.com", "ab corporation update", TOKENS) is None


def test_match_zapier_in_subject():
    assert match_company("noreply@mail.example.com", "Interview at Zapier", TOKENS) == "job-zapier"


# ---------------------------------------------------------------------------
# new_messages differ
# ---------------------------------------------------------------------------

from backend.inbox_index import new_messages

MSG_A = {"id": "msg-1", "from": "a@b.com", "subject": "Hello", "date": "2026-06-01",
         "category": "interview"}
MSG_B = {"id": "msg-2", "from": "c@d.com", "subject": "World", "date": "2026-06-02",
         "category": "rejection"}
MSG_C = {"id": "msg-3", "from": "e@f.com", "subject": "New", "date": "2026-06-03",
         "category": "interview"}


def test_new_messages_all_new():
    result = new_messages([], [MSG_A, MSG_B])
    assert result == [MSG_A, MSG_B]


def test_new_messages_none_new():
    result = new_messages([MSG_A, MSG_B], [MSG_A, MSG_B])
    assert result == []


def test_new_messages_partial():
    result = new_messages([MSG_A], [MSG_A, MSG_B, MSG_C])
    assert result == [MSG_B, MSG_C]


def test_new_messages_fallback_hash():
    """Messages without 'id' use date+from+subject hash."""
    m1 = {"from": "a@b.com", "subject": "S", "date": "2026-06-01", "category": "ack"}
    m2 = {"from": "a@b.com", "subject": "S", "date": "2026-06-01", "category": "ack"}  # same content
    m3 = {"from": "x@y.com", "subject": "Other", "date": "2026-06-02", "category": "other"}
    result = new_messages([m1], [m2, m3])
    # m2 same hash as m1 -> not new; m3 is new
    assert len(result) == 1
    assert result[0]["subject"] == "Other"


# ---------------------------------------------------------------------------
# notify() — monkeypatched httpx
# ---------------------------------------------------------------------------

def test_notify_no_op_without_token(monkeypatch):
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "telegram_bot_token", "")
    monkeypatch.setattr(cfg.settings, "telegram_chat_id", "")
    # Ensure httpx.post is NOT called when token is unset
    called = {"n": 0}
    monkeypatch.setattr("httpx.post", lambda *a, **kw: called.__setitem__("n", called["n"] + 1))
    from bot.notify import notify
    result = notify("test message")
    assert result is False
    assert called["n"] == 0


def test_notify_sends_with_token(monkeypatch):
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "telegram_bot_token", "fake-tok")
    monkeypatch.setattr(cfg.settings, "telegram_chat_id", "123")

    class FakeResp:
        def raise_for_status(self):
            pass

    calls = []
    monkeypatch.setattr("httpx.post", lambda url, json=None, timeout=None: (calls.append((url, json)) or FakeResp()))
    from bot.notify import notify
    result = notify("hello world")
    assert result is True
    assert len(calls) == 1
    url, payload = calls[0]
    assert "fake-tok" in url
    assert payload["chat_id"] == "123"
    assert payload["text"] == "hello world"
    # plain text — no parse_mode HTML
    assert "parse_mode" not in payload


def test_notify_returns_false_on_http_error(monkeypatch):
    from backend import config as cfg
    monkeypatch.setattr(cfg.settings, "telegram_bot_token", "tok")
    monkeypatch.setattr(cfg.settings, "telegram_chat_id", "999")

    def boom(*a, **kw):
        raise Exception("network error")

    monkeypatch.setattr("httpx.post", boom)
    from bot.notify import notify
    assert notify("oops") is False


# ---------------------------------------------------------------------------
# Refactored _apply_mark still delegates correctly (dashboard_app)
# ---------------------------------------------------------------------------

def test_apply_mark_delegates_to_status_store(tmp_path, monkeypatch):
    import backend.dashboard_app as dash
    import backend.status_store as ss
    monkeypatch.setattr(dash, "PREFILL_ROOT", tmp_path)
    monkeypatch.setattr(ss, "_status_path",
                        lambda profile: tmp_path / profile / "status.json")
    dash._apply_mark("alice", "job-x", "interview")
    data = json.loads((tmp_path / "alice" / "status.json").read_text())
    assert data["job-x"]["status"] == "interview"
