"""DB layer tests for the interview scheduler (backend.interviews.db).

These hit the LIVE jobfinder_crm Postgres DB (via backend.tools.mail_db's pool) —
there is no mock for this layer, it IS the schema/query contract. Every fixture uses
a throwaway login/mailbox prefix `test_iv_%` and a fixture cleans it up before AND
after the module runs so a crashed prior run never leaks rows into the next one.
If CRM_PG_DSN is unset / the DB is unreachable, the whole module is skipped.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _cur:
        _cur.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import db  # noqa: E402


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


@pytest.fixture(autouse=True)
def _clean_test_iv_rows():
    db.ensure_schema()
    _cleanup()
    yield
    _cleanup()


def _future(minutes: int) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=minutes)


def test_schema_idempotent_and_responsible_roundtrip():
    db.ensure_schema()
    db.ensure_schema()  # idempotent — must not raise the second time

    rid = db.add_responsible("test_iv_lara", "h", "Lara")
    assert isinstance(rid, int)

    fetched = db.get_responsible_by_login("test_iv_lara")
    assert fetched is not None
    assert fetched["name"] == "Lara"
    assert fetched["tz"] == "UTC"
    assert fetched["active"] is True

    by_id = db.get_responsible(rid)
    assert by_id is not None
    assert by_id["login"] == "test_iv_lara"

    roster = db.list_responsibles()
    assert rid in {r["id"] for r in roster}

    db.set_telegram_chat(rid, 987654321)
    assert db.get_responsible(rid)["telegram_chat_id"] == 987654321

    with pytest.raises(psycopg2.Error):
        db.add_responsible("test_iv_lara", "h2", "Lara Duplicate")


def test_availability_upsert_fills_missing_days():
    rid = db.add_responsible("test_iv_avail", "h", "Avail")

    rows = db.get_availability(rid)
    assert len(rows) == 7
    assert {r["dow"] for r in rows} == set(range(7))
    assert all(r["enabled"] is False for r in rows)  # nothing set yet

    db.set_availability(rid, [
        {"dow": 0, "start_min": 540, "end_min": 1020, "enabled": True},
        {"dow": 2, "start_min": 600, "end_min": 900, "enabled": True},
    ])
    rows = {r["dow"]: r for r in db.get_availability(rid)}
    assert len(rows) == 7
    assert rows[0]["enabled"] is True
    assert rows[0]["start_min"] == 540
    assert rows[0]["end_min"] == 1020
    assert rows[2]["enabled"] is True
    assert rows[1]["enabled"] is False  # untouched day still filled False

    # UPSERT: re-setting dow=0 updates in place, doesn't duplicate
    db.set_availability(rid, [{"dow": 0, "start_min": 600, "end_min": 1080, "enabled": False}])
    rows = {r["dow"]: r for r in db.get_availability(rid)}
    assert len(rows) == 7
    assert rows[0]["start_min"] == 600
    assert rows[0]["end_min"] == 1080
    assert rows[0]["enabled"] is False
    assert rows[2]["enabled"] is True  # untouched by the second call


def test_insert_interview_double_book_raises():
    rid = db.add_responsible("test_iv_dbl", "h", "Dbl")
    start = _future(60 * 24)
    end = start + timedelta(hours=1)

    first_id = db.insert_interview(
        mailbox="test_iv_p1@x.com", responsible_id=rid, start_ts=start, end_ts=end,
        company="Acme", jobid="123", thread_key="th1", source_message_hash="h1",
    )
    assert isinstance(first_id, int)

    with pytest.raises(psycopg2.Error):
        db.insert_interview(
            mailbox="test_iv_p2@x.com", responsible_id=rid, start_ts=start, end_ts=end,
            company="Beta", jobid="456", thread_key="th2", source_message_hash="h2",
        )

    # a DIFFERENT start_ts for the same responsible is not a double-book
    other_start = start + timedelta(hours=2)
    second_id = db.insert_interview(
        mailbox="test_iv_p3@x.com", responsible_id=rid, start_ts=other_start,
        end_ts=other_start + timedelta(hours=1), company="Acme", jobid="789",
        thread_key="th3", source_message_hash="h3",
    )
    assert second_id != first_id


def test_assigned_mailboxes_and_thread_lookup():
    rid = db.add_responsible("test_iv_am", "h", "AM")
    other_rid = db.add_responsible("test_iv_am2", "h", "AM2")
    start = _future(60 * 24 * 2)

    db.insert_interview(
        mailbox="test_iv_pA@x.com", responsible_id=rid, start_ts=start,
        end_ts=start + timedelta(hours=1), company="Acme", jobid="1",
        thread_key="threadA", source_message_hash="hA",
    )
    db.insert_interview(
        mailbox="test_iv_pB@x.com", responsible_id=rid, start_ts=start + timedelta(hours=3),
        end_ts=start + timedelta(hours=4), company="Acme", jobid="2",
        thread_key="threadB", source_message_hash="hB",
    )
    # a different responsible's interview must not leak into rid's set
    db.insert_interview(
        mailbox="test_iv_pC@x.com", responsible_id=other_rid, start_ts=start + timedelta(hours=5),
        end_ts=start + timedelta(hours=6), company="Beta", jobid="3",
        thread_key="threadC", source_message_hash="hC",
    )

    mboxes = db.assigned_mailboxes(rid)
    assert mboxes == {"test_iv_pA@x.com", "test_iv_pB@x.com"}

    row = db.interview_for_thread("test_iv_pA@x.com", "threadA")
    assert row is not None
    assert row["company"] == "Acme"
    assert row["responsible_id"] == rid

    assert db.interview_for_thread("test_iv_pA@x.com", "no-such-thread") is None
    assert db.interview_for_thread("test_iv_pC@x.com", "threadC") is not None


def test_due_reminders_window():
    rid = db.add_responsible("test_iv_rem", "h", "Rem")
    now = datetime.now(timezone.utc)

    soon = now + timedelta(minutes=3)     # inside BOTH the 5-min and 60-min windows
    mid = now + timedelta(minutes=50)     # inside the 60-min window only
    past_window = now + timedelta(minutes=120)  # outside both windows

    soon_id = db.insert_interview(
        mailbox="test_iv_soon@x.com", responsible_id=rid, start_ts=soon,
        end_ts=soon + timedelta(hours=1), company="Acme", jobid="s1",
        thread_key="ts", source_message_hash="hs",
    )
    mid_id = db.insert_interview(
        mailbox="test_iv_mid@x.com", responsible_id=rid, start_ts=mid,
        end_ts=mid + timedelta(hours=1), company="Acme", jobid="s2",
        thread_key="tm", source_message_hash="hm",
    )
    db.insert_interview(
        mailbox="test_iv_far@x.com", responsible_id=rid, start_ts=past_window,
        end_ts=past_window + timedelta(hours=1), company="Acme", jobid="s3",
        thread_key="tf", source_message_hash="hf",
    )

    due5 = {r["id"] for r in db.due_reminders(now, 5)}
    assert soon_id in due5
    assert mid_id not in due5

    due60 = {r["id"] for r in db.due_reminders(now, 60)}
    assert soon_id in due60
    assert mid_id in due60

    db.mark_reminded(soon_id, "5")
    due5_after = {r["id"] for r in db.due_reminders(now, 5)}
    assert soon_id not in due5_after
    # marking the 5-min flag must not affect the independent 60-min flag
    due60_after = {r["id"] for r in db.due_reminders(now, 60)}
    assert soon_id in due60_after

    db.mark_reminded(soon_id, "60")
    due60_after2 = {r["id"] for r in db.due_reminders(now, 60)}
    assert soon_id not in due60_after2
