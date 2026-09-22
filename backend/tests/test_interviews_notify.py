"""Tests for the interview-scheduler notifier daemon.

The pure/monkeypatched tests (message target resolution, the pure `plan`, and a
fully-monkeypatched `tick`) hit NO DB and NEVER touch Telegram — `send_dm` /
`notify_responsible` are always monkeypatched. The two live-DB tests (announcement
roundtrip) use the throwaway `test_iv_%` prefix and skip when CRM_PG_DSN is unset.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
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
    # tick() now fires FOUR reminder windows (-120/-60/-15/-5) plus the assignment announce.
    # -120 and -15 are ADMIN-facing (send_admin); assigned/-60/-5 go to the responsible bot.
    from backend.interviews import service
    state = {"announced": False, "60": False, "5": False, "120": False, "15": False}
    marks = {"announced": [], "reminded": []}
    resp_sends: list = []
    admin_sends: list = []

    monkeypatch.setattr(db, "due_announcements",
                        lambda: [] if state["announced"] else [{"id": 1, "responsible_id": 10}])

    def fake_due_reminders(now, window):
        w = str(int(window))
        return [] if state[w] else [{"id": int(w), "responsible_id": 10}]

    monkeypatch.setattr(db, "due_reminders", fake_due_reminders)
    monkeypatch.setattr(db, "get_responsible", lambda rid: {"name": "R"})

    def fake_mark_announced(iid):
        marks["announced"].append(iid)
        state["announced"] = True

    def fake_mark_reminded(iid, which):
        marks["reminded"].append((iid, which))
        state[which] = True

    monkeypatch.setattr(db, "mark_announced", fake_mark_announced)
    monkeypatch.setattr(db, "mark_reminded", fake_mark_reminded)
    monkeypatch.setattr(notify, "poll_updates", lambda: 0)  # no Telegram network in tests
    # hermetic: record which bot each pair went to; never touch the network / build a pack
    monkeypatch.setattr(notify, "notify_responsible",
                        lambda iv, text: (resp_sends.append(iv["id"]), True)[1])
    monkeypatch.setattr(notify, "send_admin", lambda text: admin_sends.append(text) or True)
    monkeypatch.setattr(notify, "send_document", lambda *a, **k: True)
    monkeypatch.setattr(notify, "target_chat", lambda iv: None)
    monkeypatch.setattr(notify, "assigned_text", lambda *a, **k: "assigned")
    monkeypatch.setattr(notify, "walkin_prep_text", lambda *a, **k: "walkin")
    monkeypatch.setattr(notify, "rich_reminder_text", lambda *a, **k: "rich")
    monkeypatch.setattr(notify, "reminder_text", lambda *a, **k: "rem")
    monkeypatch.setattr(service, "interview_pack", lambda iv: {})
    # the ADDITIVE weekly nudge is tested separately; keep this tick hermetic regardless of
    # the day/hour it runs (it must not reach the DB or Telegram here).
    monkeypatch.setattr(reminders, "_maybe_weekly_nudge", lambda now: 0)

    attempted = reminders.tick()
    # 1 announce + 4 reminders (120/60/15/5)
    assert attempted == 5
    assert sorted(resp_sends) == [1, 5, 60]      # assigned + -60 + -5 → responsible bot
    assert len(admin_sends) == 2                 # -120 + -15 → admin bot
    assert marks["announced"] == [1]
    assert set(marks["reminded"]) == {(60, "60"), (5, "5"), (120, "120"), (15, "15")}

    # second tick: everything already marked -> nothing sent
    resp_sends.clear()
    admin_sends.clear()
    assert reminders.tick() == 0
    assert resp_sends == [] and admin_sends == []


def test_message_builders_are_neutral():
    iv = {"mailbox": "jane.doe7@takhet.com", "company": "Acme",
          "start_ts": datetime(2026, 8, 30, 14, 30, tzinfo=timezone.utc)}
    a = notify.assigned_text(iv, "Alan")
    r = notify.reminder_text(iv, "Alan", 60)
    for text in (a, r):
        assert "Alan" in text
        assert "jane.doe7" in text          # persona local-part
        assert "Acme" in text
        assert "2026-08-30 19:30 (Almaty)" in text   # 14:30 UTC == 19:30 Almaty (+5)
        low = text.lower()
        for banned in ("claude", "anthropic", "gpt", "openai", "llm"):
            assert banned not in low
    assert "60" in r


def test_when_renders_in_local_almaty_regardless_of_input_offset():
    # any aware datetime displays as Almaty (+5) wall-clock; here 15:00+05 == 10:00 UTC
    iv = {"mailbox": "x@takhet.com", "company": "Acme",
          "start_ts": datetime(2026, 8, 31, 15, 0, 0,
                               tzinfo=timezone(timedelta(hours=5)))}
    text = notify.assigned_text(iv, "Alan")
    assert "2026-08-31 15:00 (Almaty)" in text   # Almaty local, not the 10:00 UTC hour
    assert "10:00" not in text


def test_when_naive_datetime_assumed_utc_shown_in_almaty():
    iv = {"mailbox": "x@takhet.com", "company": "Acme",
          "start_ts": datetime(2026, 8, 31, 9, 30, 0)}  # tz-naive -> assumed UTC
    assert "2026-08-31 14:30 (Almaty)" in notify.reminder_text(iv, "Alan", 5)  # 09:30 UTC +5


_SECRET_TOKEN = "123456789:AAF_super_secret_bot_token_value"


def test_bot_token_prefers_iv_over_main(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "telegram_bot_token", "MAIN")
    monkeypatch.setattr(settings, "iv_bot_token", "IVTOK")
    assert notify._bot_token() == "IVTOK"
    # dedicated token cleared -> fall back to the project-wide bot token
    monkeypatch.setattr(settings, "iv_bot_token", "")
    assert notify._bot_token() == "MAIN"


def test_send_dm_http_failure_does_not_log_token(monkeypatch, caplog):
    from backend.config import settings
    monkeypatch.setattr(settings, "iv_bot_token", "")   # deterministic: use the set main token
    monkeypatch.setattr(settings, "telegram_bot_token", _SECRET_TOKEN)

    class _Resp:
        status_code = 400
        text = '{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: _Resp())

    with caplog.at_level(logging.WARNING, logger=notify.logger.name):
        assert notify.send_dm(555, "hi") is False

    assert caplog.records, "expected a warning to be logged on a failed send"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in joined
    assert "AAF_super_secret" not in joined
    assert "chat not found" in joined       # the useful, token-free diagnostic


def test_send_dm_transport_error_does_not_log_token(monkeypatch, caplog):
    from backend.config import settings
    monkeypatch.setattr(settings, "iv_bot_token", "")   # deterministic: use the set main token
    monkeypatch.setattr(settings, "telegram_bot_token", _SECRET_TOKEN)

    def _boom(*a, **k):
        # httpx errors stringify the request URL, which embeds the token
        raise httpx.ConnectError(
            f"connection failed to https://api.telegram.org/bot{_SECRET_TOKEN}/sendMessage")

    monkeypatch.setattr(notify.httpx, "post", _boom)

    with caplog.at_level(logging.WARNING, logger=notify.logger.name):
        assert notify.send_dm(555, "hi") is False

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in joined
    assert "AAF_super_secret" not in joined
    assert "ConnectError" in joined         # only the exception TYPE is logged


# ---- weekly «раскидай интервью на неделю» nudge (pure / monkeypatched) -------------
def test_weekly_nudge_text_manager_mentions_10min_count_and_manage_path():
    resp = {"name": "Мадина", "roles": ["manager"]}
    t = notify.weekly_nudge_text(resp, 7, "https://jobs.systeam.kz")
    assert "10 минут" in t
    assert "7" in t                       # the pending count
    assert "/manage" in t and "/cabinet" not in t
    assert "команде" in t                 # the manager-specific ask
    low = t.lower()
    for banned in ("claude", "anthropic", "gpt", "openai", "llm", " ai ", "ии", "chatgpt"):
        assert banned not in low


def test_weekly_nudge_text_interviewer_uses_cabinet_path():
    resp = {"name": "Sam", "roles": ["employee"]}
    t = notify.weekly_nudge_text(resp, 3, "https://jobs.systeam.kz/")   # trailing slash tolerated
    assert "10 минут" in t
    assert "3" in t
    assert "/cabinet" in t and "/manage" not in t
    assert "Подготовься" in t
    low = t.lower()
    for banned in ("claude", "anthropic", "gpt", "openai", "llm"):
        assert banned not in low


def test_weekly_nudge_text_manager_wins_when_multirole():
    # a manager+employee is routed to the higher (manager) surface
    resp = {"name": "R", "roles": ["employee", "manager"]}
    assert "/manage" in notify.weekly_nudge_text(resp, 1, "https://x")


def test_weekly_nudge_text_shows_unscheduled_only_when_positive():
    resp = {"name": "R", "roles": ["employee"]}
    assert "без назначенного времени" in notify.weekly_nudge_text(resp, 5, "https://x", 2).lower()
    assert "без назначенного времени" not in notify.weekly_nudge_text(resp, 5, "https://x", 0).lower()


def test_send_weekly_nudge_counts_sends_and_picks_source_by_role(monkeypatch):
    sent: list = []
    recips = [
        {"id": 1, "name": "Mgr", "roles": ["manager"], "telegram_chat_id": 111},
        {"id": 2, "name": "Emp", "roles": ["employee"], "telegram_chat_id": 222},
        {"id": 3, "name": "NoChat", "roles": ["employee"], "telegram_chat_id": None},
    ]
    monkeypatch.setattr(db, "responsibles_for_nudge", lambda: recips)
    held, upcoming = [], []

    def fake_held(uid, by="responsible"):
        held.append((uid, by))
        return [{"start_ts": 1}, {"start_ts": None}]

    def fake_upcoming(uid, upcoming_only=False):
        upcoming.append((uid, upcoming_only))
        return [{"start_ts": 1}]

    monkeypatch.setattr(db, "interviews_held_by", fake_held)
    monkeypatch.setattr(db, "interviews_for_responsible", fake_upcoming)
    monkeypatch.setattr(notify, "send_dm", lambda chat_id, text: (sent.append(chat_id), True)[1])

    n = reminders.send_weekly_nudge(datetime.now(timezone.utc))
    assert n == 2                          # the two linked recipients; the no-chat one skipped
    assert sent == [111, 222]
    assert held == [(1, "responsible")]    # manager → held_by(responsible)
    assert upcoming == [(2, True)]         # interviewer → for_responsible(upcoming_only=True)


def test_maybe_weekly_nudge_day_hour_and_dedupe_gate(monkeypatch, tmp_path):
    marker = tmp_path / "iv_weekly_nudge.json"
    monkeypatch.setattr(reminders, "_NUDGE_MARKER", marker)
    calls: list = []
    monkeypatch.setattr(reminders, "send_weekly_nudge",
                        lambda now: (calls.append(now), 4)[1])

    # a Friday 19:05 Almaty == 14:05 UTC (Almaty is +5, no DST)
    fri = datetime(2026, 9, 25, 14, 5, tzinfo=timezone.utc)   # 2026-09-25 is a Friday
    assert reminders._maybe_weekly_nudge(fri) == 4
    assert len(calls) == 1
    # the 60s loop fires again the same evening -> the marker blocks a second send
    assert reminders._maybe_weekly_nudge(datetime(2026, 9, 25, 14, 6, tzinfo=timezone.utc)) == 0
    assert len(calls) == 1
    # a fresh daemon (re-read the on-disk marker) still won't re-send that day
    assert reminders._read_nudge_marker() == "2026-09-25:fri"

    # a Wednesday, or a Friday before 19:00 local, is NOT a broadcast window
    wed = datetime(2026, 9, 23, 14, 5, tzinfo=timezone.utc)   # Wednesday
    assert reminders._maybe_weekly_nudge(wed) == 0
    early_fri = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)  # 15:00 Almaty < 19:00
    assert reminders._maybe_weekly_nudge(early_fri) == 0
    assert len(calls) == 1

    # Sunday 19:00 local is a fresh window with its own key
    sun = datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)   # 19:00 Almaty, Sunday
    assert reminders._maybe_weekly_nudge(sun) == 4
    assert reminders._read_nudge_marker() == "2026-09-27:sun"
    assert len(calls) == 2


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


def test_reminder_uses_responsible_timezone():
    # 13:00 UTC == 09:00 New York (EDT); the reminder shows the responsible's own zone
    iv = {"mailbox": "x@takhet.com", "company": "Acme",
          "start_ts": datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)}
    t = notify.reminder_text(iv, "Sam", 60, "America/New_York")
    assert "2026-08-31 09:00 (New York)" in t


def test_rich_reminder_text_has_all_fields():
    iv = {"mailbox": "charles.morin6978@takhet.com", "company": "Acme", "responsible_id": 1,
          "start_ts": datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)}  # 13:00 UTC == 09:00 NY
    pack = {"company": "Acme Corp", "title": "Sales Manager", "persona_name": "Charles Morin",
            "resume_path": "/x/resume.pdf", "zoom": "https://zoom.us/j/123"}
    t = notify.rich_reminder_text(iv, "Sam", "America/New_York", pack)
    for s in ("Charles Morin", "Acme Corp", "Sales Manager", "https://zoom.us/j/123", "09:00 (New York)"):
        assert s in t, s
    # no-link fallback line
    assert "не найдена" in notify.rich_reminder_text(iv, "Sam", None, {**pack, "zoom": ""})
