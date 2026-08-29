"""Service-layer tests for the interview scheduler (backend.interviews.service).

Hits the LIVE jobfinder_crm Postgres DB (same as test_interviews_db.py) — a
`test_iv_%` login/mailbox prefix is used throughout and cleaned up before AND
after the module runs. Skipped entirely if the DB is unreachable.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _cur:
        _cur.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import db, service, slots  # noqa: E402


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


def _next_monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def test_grid_marks_free_cells():
    monday = _next_monday()
    # member and viewer both in Almaty here (the cross-tz case is in test_interviews_slots)
    rid = db.add_responsible("test_iv_grid", "h", "Grid", tz="Asia/Almaty")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},  # 09:00-17:00 local
    ])

    grid = service.grid_for_week(monday, "Asia/Almaty")

    assert grid["hours"] == list(range(slots.HOUR_START, slots.HOUR_END))
    assert grid["dates"] == [d.isoformat() for d in slots.week_dates(monday)]
    assert {"id": rid, "name": "Grid"} in grid["responsibles"]

    def ids(key):
        return [e["id"] for e in grid["cells"][key]]

    assert rid in ids(f"{monday.isoformat()}:10")       # 10:00 Almaty is in the window
    assert rid not in ids(f"{monday.isoformat()}:08")   # 08:00 is before it opens
    tuesday = monday + timedelta(days=1)
    assert rid not in ids(f"{tuesday.isoformat()}:10")  # no enabled row for Tuesday

    # the entry carries the member's OWN local time + tz label
    entry = next(e for e in grid["cells"][f"{monday.isoformat()}:10"] if e["id"] == rid)
    assert entry["name"] == "Grid" and entry["local"] == "10:00" and entry["tz"] == "Almaty"


def test_assign_books_and_links_mailbox():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_assign", "h", "Assign")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])
    start = slots.cell_start_utc("UTC", monday, 10)
    mailbox = "test_iv_candidate@takhet.com"

    row = service.assign(
        mailbox=mailbox, responsible_id=rid, start_iso=start.isoformat(),
        company="Acme", jobid="1234", thread_key="thread-1",
        source_message_hash="hash-1",
    )

    assert row is not None
    assert row["id"] is not None
    assert mailbox in db.assigned_mailboxes(rid)

    linked = db.interview_for_thread(mailbox, "thread-1")
    assert linked is not None
    assert linked["responsible_id"] == rid
    assert linked["company"] == "Acme"


def test_assign_conflict_raises():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_conflict", "h", "Conflict")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])
    start = slots.cell_start_utc("UTC", monday, 11)

    service.assign(
        mailbox="test_iv_first@takhet.com", responsible_id=rid,
        start_iso=start.isoformat(), company="Acme", jobid="1", thread_key="t1",
        source_message_hash="h1",
    )

    with pytest.raises(service.SlotConflict):
        service.assign(
            mailbox="test_iv_second@takhet.com", responsible_id=rid,
            start_iso=start.isoformat(), company="Beta", jobid="2", thread_key="t2",
            source_message_hash="h2",
        )


def test_assign_outside_availability_raises_conflict():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_outside", "h", "Outside")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])
    start = slots.cell_start_utc("UTC", monday, 8)  # before the 09:00 window opens

    with pytest.raises(service.SlotConflict):
        service.assign(
            mailbox="test_iv_early@takhet.com", responsible_id=rid,
            start_iso=start.isoformat(), company="Acme", jobid="1", thread_key="t1",
            source_message_hash="h1",
        )


def test_assign_naive_start_iso_assumed_utc():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_naive", "h", "Naive")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])
    start = slots.cell_start_utc("UTC", monday, 12)
    naive_iso = start.replace(tzinfo=None).isoformat()

    row = service.assign(
        mailbox="test_iv_naive_cand@takhet.com", responsible_id=rid,
        start_iso=naive_iso, company="Acme", jobid="1", thread_key="t1",
        source_message_hash="h1",
    )
    assert row is not None
    assert row["start_ts"] == start


def test_assign_rejects_non_hour_aligned():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_align", "h", "Align")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])
    start = slots.cell_start_utc("UTC", monday, 10)

    # hour-aligned start at a free slot succeeds
    row = service.assign(
        mailbox="test_iv_align_ok@takhet.com", responsible_id=rid,
        start_iso=start.isoformat(), company="Acme", jobid="1", thread_key="t1",
        source_message_hash="h1",
    )
    assert row is not None

    # a :45 / :30 start in the SAME hour overlaps the booked [10:00,11:00) and
    # is rejected by is_free_at's exact-window overlap check
    quarter_past = (start + timedelta(minutes=45)).isoformat()
    with pytest.raises(service.SlotConflict):
        service.assign(
            mailbox="test_iv_align_45@takhet.com", responsible_id=rid,
            start_iso=quarter_past, company="Beta", jobid="2", thread_key="t2",
            source_message_hash="h2",
        )

    # a :30 start is likewise rejected
    half_past = (start + timedelta(minutes=30)).isoformat()
    with pytest.raises(service.SlotConflict):
        service.assign(
            mailbox="test_iv_align_30@takhet.com", responsible_id=rid,
            start_iso=half_past, company="Gamma", jobid="3", thread_key="t3",
            source_message_hash="h3",
        )


def test_assign_offset_start_iso_converted_and_aligned_to_utc():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_offset", "h", "Offset")
    # 06:00 UTC == dow(monday)=0, so its availability window covers hour=6
    db.set_availability(rid, [
        {"dow": 0, "start_min": 0, "end_min": 24 * 60, "enabled": True},
    ])

    # 11:00+05:00 == 06:00 UTC, hour-aligned -> accepted
    offset_iso = f"{monday.isoformat()}T11:00:00+05:00"
    row = service.assign(
        mailbox="test_iv_offset_ok@takhet.com", responsible_id=rid,
        start_iso=offset_iso, company="Acme", jobid="1", thread_key="t1",
        source_message_hash="h1",
    )
    assert row is not None
    expected_start = slots.cell_start_utc("UTC", monday, 6)  # 11:00+05 == 06:00 UTC
    assert row["start_ts"] == expected_start
    # the DB session's timezone (e.g. Europe/Berlin) is NOT UTC, so a fetched
    # tz-aware datetime must be normalized to UTC before reading .hour
    assert row["start_ts"].astimezone(timezone.utc).hour == 6

    # a SUB-MINUTE start (seconds) is rejected (whole-minute alignment guard)
    offset_iso_seconds = f"{monday.isoformat()}T11:00:30+05:00"
    with pytest.raises(service.SlotConflict):
        service.assign(
            mailbox="test_iv_offset_bad@takhet.com", responsible_id=rid,
            start_iso=offset_iso_seconds, company="Beta", jobid="2",
            thread_key="t2", source_message_hash="h2",
        )


def test_assign_reassigns_and_cancels_prior_booking():
    monday = _next_monday()
    r1 = db.add_responsible("test_iv_re1", "h", "Re One")
    r2 = db.add_responsible("test_iv_re2", "h", "Re Two")
    for rid in (r1, r2):
        db.set_availability(rid, [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}])
    mailbox = "test_iv_reassign@takhet.com"
    s1 = slots.cell_start_utc("UTC", monday, 10)
    s2 = slots.cell_start_utc("UTC", monday, 12)

    service.assign(mailbox=mailbox, responsible_id=r1, start_iso=s1.isoformat(),
                   company="Acme", jobid="1", thread_key="thr", source_message_hash="h1")
    assert db.active_interview_for_thread(mailbox, "thr")["responsible_id"] == r1

    # reassign to r2 at a different slot on the SAME thread — replaces the prior booking
    service.assign(mailbox=mailbox, responsible_id=r2, start_iso=s2.isoformat(),
                   company="Acme", jobid="1", thread_key="thr", source_message_hash="h1")

    active = db.active_interview_for_thread(mailbox, "thr")
    assert active["responsible_id"] == r2
    assert active["start_ts"].astimezone(timezone.utc).hour == 12
    # exactly ONE active interview remains for the thread (the old one is cancelled)
    with mail_db._cur(dict_rows=False) as c:
        c.execute("SELECT count(*) FROM iv_interviews WHERE mailbox=%s AND thread_key=%s "
                  "AND status<>'cancelled'", (mailbox, "thr"))
        assert c.fetchone()[0] == 1
    assert db.assignments_for_mailboxes([mailbox])[mailbox]["responsible_name"] == "Re Two"


def test_cancel_clears_assignment():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_cancel", "h", "Cancel")
    db.set_availability(rid, [{"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True}])
    mailbox = "test_iv_cancel_cand@takhet.com"
    s = slots.cell_start_utc("UTC", monday, 14)
    service.assign(mailbox=mailbox, responsible_id=rid, start_iso=s.isoformat(),
                   company="Acme", jobid="1", thread_key="tc", source_message_hash="h")
    assert db.active_interview_for_thread(mailbox, "tc") is not None

    assert service.cancel(mailbox, "tc") == 1
    assert db.active_interview_for_thread(mailbox, "tc") is None
    assert db.assignments_for_mailboxes([mailbox]) == {}


def test_mailbox_context_defaults_when_no_match():
    ctx = service.mailbox_context("no-such-mailbox-ever@takhet.com")
    assert ctx == {"company": "", "jobid": ""}


def test_mailbox_context_finds_real_persona():
    import glob
    import json as _json

    matches = glob.glob("uploads/prefill/*/*/persona.json")
    found = None
    for pj in matches:
        try:
            with open(pj, encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            continue
        email = (data.get("profile") or {}).get("email")
        if email:
            found = (pj, email)
            break
    if not found:
        pytest.skip("no persona.json with an email present in uploads/prefill")

    pj, email = found
    ctx = service.mailbox_context(email)
    assert ctx["jobid"] == pj.split("/")[-2]


def test_meeting_regex_matches_links_not_noise():
    from backend.interviews.service import _MEETING_RE
    assert _MEETING_RE.search("Join https://zoom.us/j/8899001122?pwd=abc please")
    assert _MEETING_RE.search("Meet at https://meet.google.com/abc-defg-hij tomorrow")
    assert _MEETING_RE.search("Teams: https://teams.microsoft.com/l/meetup-join/xyz here")
    assert not _MEETING_RE.search("Visit https://example.com/careers for info")
