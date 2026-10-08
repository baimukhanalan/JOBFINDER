"""Tests for the isolated daily auto-assign + evening-slot reservation lane (auto_reserve.py).

Pure slot logic needs no DB. The reservation path hits the LIVE jobfinder_crm (skipped if
unreachable); every seeded row uses a `test_iv_%` prefix, cleaned up + forced announced=TRUE on
teardown so the live ivremind notifier never pings the owner. Run SEQUENTIALLY with the other
`test_interviews_*` (shared live DB)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.interviews import auto_reserve as ar


# ------------------------------------------------------------------ pure slot logic (no DB)
def test_slot_order_centres_on_2000():
    order = ar._slot_order()
    assert order[0] == 20 * 60                       # 20:00 is first (closest to target)
    assert set(order) == set(range(ar.WINDOW_START_MIN, ar.WINDOW_END_MIN + 1, ar.SLOT_STEP_MIN))
    assert order[1] in (19 * 60 + 30, 20 * 60 + 30)  # then the ±30-min neighbours


def test_candidate_slots_are_future_evening_utc():
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
    slots = ar.candidate_slots(tz, days=3, start_offset=1, now=now)
    assert slots, "should yield slots"
    assert all(s.tzinfo is not None for s in slots)                 # tz-aware
    first_local = slots[0].astimezone(tz)
    assert first_local.date() == now.astimezone(tz).date() + timedelta(days=1)  # starts tomorrow
    assert (first_local.hour, first_local.minute) == (20, 0)        # nearest to 20:00 first
    assert all(ar.WINDOW_START_MIN <= s.astimezone(tz).hour * 60 + s.astimezone(tz).minute
               <= ar.WINDOW_END_MIN for s in slots)


# ------------------------------------------------------------------ live-DB reservation path
try:
    from backend.tools import mail_db
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
    _DB = True
except Exception:
    _DB = False

pytestmark = pytest.mark.skipif(not _DB, reason="no CRM DB")

if _DB:
    from backend.interviews import auth, db


def _retry(fn, tries=6):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_ar_%'")
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_ar_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_ar_%'")


@pytest.fixture()
def seeded(monkeypatch):
    _retry(db.ensure_schema)
    _retry(_cleanup)
    rid = _retry(lambda: db.add_responsible("test_iv_ar_R", auth.hash_password("x"),
                                            "AR Tester", tz="Europe/Berlin", role="manager"))
    # two unscheduled собесы assigned to the test responsible
    ids = []
    for i in range(2):
        ids.append(_retry(lambda i=i: db.insert_interview(
            mailbox=f"test_iv_ar_{i}@takhet.com", responsible_id=rid, start_ts=None, end_ts=None,
            company="Acme", jobid=str(i), thread_key="t", source_message_hash=f"h{i}")))
    monkeypatch.setattr(ar, "RESPONSIBLE_ID", rid)
    monkeypatch.setattr(ar, "_pick_fresh_pool_mailbox", lambda: None)   # don't pull real pool rows
    monkeypatch.setattr(ar, "_STATE", "/tmp/_ar_state_test.json")
    monkeypatch.setattr(ar, "_MANUAL", "/tmp/_ar_manual_test.json")
    yield rid, ids
    _retry(_cleanup)


def test_dry_run_changes_nothing(seeded):
    rid, ids = seeded
    res = ar.run(dry_run=True)
    assert len(res["reserved"]) == 2
    for iid in ids:
        assert _retry(lambda iid=iid: db.interview_by_id(iid))["start_ts"] is None   # untouched


def test_reserve_sets_evening_slots_and_suppresses_notifications(seeded):
    rid, ids = seeded
    res = ar.run(dry_run=False, notify=False)
    assert len(res["reserved"]) == 2
    tz = ZoneInfo("Europe/Berlin")
    seen = set()
    for iid in ids:
        row = _retry(lambda iid=iid: db.interview_by_id(iid))
        assert row["start_ts"] is not None
        local = row["start_ts"].astimezone(tz)
        mins = local.hour * 60 + local.minute
        assert ar.WINDOW_START_MIN <= mins <= ar.WINDOW_END_MIN     # inside 18:30–21:30
        assert row["announced"] is True and row["reminded_5"] is True  # notifications suppressed
        assert row["start_ts"] not in seen                          # no double-book
        seen.add(row["start_ts"])


def test_reserve_is_idempotent(seeded):
    rid, ids = seeded
    first = ar.run(dry_run=False)
    starts = {iid: _retry(lambda iid=iid: db.interview_by_id(iid))["start_ts"] for iid in ids}
    second = ar.run(dry_run=False)                 # nothing left unscheduled → no-op
    assert second["reserved"] == []
    for iid in ids:
        assert _retry(lambda iid=iid: db.interview_by_id(iid))["start_ts"] == starts[iid]
