"""Tests for the interview-scheduler notifier daemon.

The pure/monkeypatched tests (message target resolution, the pure `plan`, and a
fully-monkeypatched `tick`) hit NO DB and NEVER touch Telegram — `send_dm` /
`notify_responsible` are always monkeypatched. The two live-DB tests (announcement
roundtrip) use the throwaway `test_iv_%` prefix and skip when CRM_PG_DSN is unset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.tools import mail_db
from backend.interviews import db, notify, reminders

try:
    with mail_db._cur(dict_rows=False) as _cur:
        _cur.execute("SELECT 1")
    HAS_DB = True
except Exception:
    HAS_DB = False


# ---- pure / monkeypatched (no DB, no network) --------------------------------------
def test_notify_responsible_prefers_personal_then_owner(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "telegram_chat_id", 999)

    sent: list = []

    # 1) personal chat set + personal send succeeds -> personal used
    monkeypatch.setattr(db, "get_responsible",
                        lambda rid: {"name": "R", "telegram_chat_id": 111})
    monkeypatch.setattr(notify, "send_dm",
                        lambda chat_id, text: (sent.append(chat_id), True)[1])
    assert notify.notify_responsible({"responsible_id": 1}, "hi") is True
    assert sent == [111]

    # 2) personal chat unset -> owner chat used
    sent.clear()
    monkeypatch.setattr(db, "get_responsible",
                        lambda rid: {"name": "R", "telegram_chat_id": None})
    assert notify.notify_responsible({"responsible_id": 1}, "hi") is True
    assert sent == [999]

    # 3) personal chat set but its send fails -> falls back to owner chat
    sent.clear()
    monkeypatch.setattr(db, "get_responsible",
                        lambda rid: {"name": "R", "telegram_chat_id": 111})

    def fail_personal(chat_id, text):
        sent.append(chat_id)
        return chat_id != 111  # fail for the personal chat, succeed for the owner

    monkeypatch.setattr(notify, "send_dm", fail_personal)
    assert notify.notify_responsible({"responsible_id": 1}, "hi") is True
    assert sent == [111, 999]


def test_plan_labels_all_kinds():
    a, b, c = {"id": 1}, {"id": 2}, {"id": 3}
    assert reminders.plan([a], [b], [c]) == [
        (a, "assigned"), (b, "60"), (c, "5"),
    ]


def test_plan_empty():
    assert reminders.plan([], [], []) == []


def test_tick_sends_and_marks_idempotent(monkeypatch):
    state = {"announced": False, "r60": False, "r5": False}
    marks = {"announced": [], "reminded": []}
    sends: list = []

    monkeypatch.setattr(db, "due_announcements",
                        lambda: [] if state["announced"] else [{"id": 1, "responsible_id": 10}])

    def fake_due_reminders(now, window):
        if int(window) == 60:
            return [] if state["r60"] else [{"id": 2, "responsible_id": 10}]
        return [] if state["r5"] else [{"id": 3, "responsible_id": 10}]

    monkeypatch.setattr(db, "due_reminders", fake_due_reminders)
    monkeypatch.setattr(db, "get_responsible", lambda rid: {"name": "R"})

    def fake_mark_announced(iid):
        marks["announced"].append(iid)
        state["announced"] = True

    def fake_mark_reminded(iid, which):
        marks["reminded"].append((iid, which))
        state["r60" if which == "60" else "r5"] = True

    monkeypatch.setattr(db, "mark_announced", fake_mark_announced)
    monkeypatch.setattr(db, "mark_reminded", fake_mark_reminded)
    monkeypatch.setattr(notify, "notify_responsible",
                        lambda iv, text: (sends.append((iv["id"], text)), True)[1])

    attempted = reminders.tick()
    assert attempted == 3
    assert len(sends) == 3
    assert marks["announced"] == [1]
    assert set(marks["reminded"]) == {(2, "60"), (3, "5")}

    # second tick: everything already marked -> nothing sent
    sends.clear()
    assert reminders.tick() == 0
    assert sends == []


def test_message_builders_are_neutral():
    iv = {"mailbox": "jane.doe7@takhet.com", "company": "Acme",
          "start_ts": datetime(2026, 8, 30, 14, 30, tzinfo=timezone.utc)}
    a = notify.assigned_text(iv, "Alan")
    r = notify.reminder_text(iv, "Alan", 60)
    for text in (a, r):
        assert "Alan" in text
        assert "jane.doe7" in text          # persona local-part
        assert "Acme" in text
        assert "2026-08-30 14:30 GMT" in text
        low = text.lower()
        for banned in ("claude", "anthropic", "gpt", "openai", "llm"):
            assert banned not in low
    assert "60" in r


# ---- live DB (announcement roundtrip) ----------------------------------------------
def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


@pytest.fixture()
def _clean_test_iv_rows():
    db.ensure_schema()
    _cleanup()
    yield
    _cleanup()


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_due_announcements_roundtrip(_clean_test_iv_rows):
    rid = db.add_responsible("test_iv_ann", "h", "Ann")
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    iid = db.insert_interview(
        mailbox="test_iv_ann@x.com", responsible_id=rid, start_ts=start,
        end_ts=start + timedelta(hours=1), company="Acme", jobid="1",
        thread_key="tA", source_message_hash="hA",
    )
    ids = {r["id"] for r in db.due_announcements()}
    assert iid in ids

    db.mark_announced(iid)
    ids_after = {r["id"] for r in db.due_announcements()}
    assert iid not in ids_after


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_mark_announced(_clean_test_iv_rows):
    rid = db.add_responsible("test_iv_ann2", "h", "Ann2")
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    iid = db.insert_interview(
        mailbox="test_iv_ann2@x.com", responsible_id=rid, start_ts=start,
        end_ts=start + timedelta(hours=1), company="Beta", jobid="2",
        thread_key="tB", source_message_hash="hB",
    )
    assert iid in {r["id"] for r in db.due_announcements()}
    db.mark_announced(iid)
    assert iid not in {r["id"] for r in db.due_announcements()}
