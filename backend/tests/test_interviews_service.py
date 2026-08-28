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
    rid = db.add_responsible("test_iv_grid", "h", "Grid")
    # dow=monday.weekday() == 0, available 09:00-17:00 UTC
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])

    grid = service.grid_for_week(monday)

    assert grid["hours"] == list(range(slots.HOUR_START, slots.HOUR_END))
    assert grid["dates"] == [d.isoformat() for d in slots.week_dates(monday)]
    assert {"id": rid, "name": "Grid"} in grid["responsibles"]

    free_key = f"{monday.isoformat()}:10"
    assert {"id": rid, "name": "Grid"} in grid["cells"][free_key]

    # 08:00 is outside the 09:00-17:00 window -> not free
    busy_key = f"{monday.isoformat()}:08"
    assert {"id": rid, "name": "Grid"} not in grid["cells"][busy_key]

    # Tuesday has no availability row set to enabled -> not free
    tuesday = monday + timedelta(days=1)
    tue_key = f"{tuesday.isoformat()}:10"
    assert {"id": rid, "name": "Grid"} not in grid["cells"][tue_key]


def test_assign_books_and_links_mailbox():
    monday = _next_monday()
    rid = db.add_responsible("test_iv_assign", "h", "Assign")
    db.set_availability(rid, [
        {"dow": 0, "start_min": 9 * 60, "end_min": 17 * 60, "enabled": True},
    ])
    start = slots.cell_start_utc(monday, 10)
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
    start = slots.cell_start_utc(monday, 11)

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
    start = slots.cell_start_utc(monday, 8)  # before the 09:00 window opens

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
    start = slots.cell_start_utc(monday, 12)
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
    start = slots.cell_start_utc(monday, 10)

    # hour-aligned start at a free slot succeeds
    row = service.assign(
        mailbox="test_iv_align_ok@takhet.com", responsible_id=rid,
        start_iso=start.isoformat(), company="Acme", jobid="1", thread_key="t1",
        source_message_hash="h1",
    )
    assert row is not None

    # a :45 start in the SAME hour must be rejected by the alignment guard
    # (not silently pass is_free and slip a real overlap past the DB guard)
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
    expected_start = slots.cell_start_utc(monday, 6)
    assert row["start_ts"] == expected_start
    # the DB session's timezone (e.g. Europe/Berlin) is NOT UTC, so a fetched
    # tz-aware datetime must be normalized to UTC before reading .hour
    assert row["start_ts"].astimezone(timezone.utc).hour == 6

    # 11:15+05:00 == 06:15 UTC, NOT hour-aligned -> rejected
    offset_iso_misaligned = f"{monday.isoformat()}T11:15:00+05:00"
    with pytest.raises(service.SlotConflict):
        service.assign(
            mailbox="test_iv_offset_bad@takhet.com", responsible_id=rid,
            start_iso=offset_iso_misaligned, company="Beta", jobid="2",
            thread_key="t2", source_message_hash="h2",
        )


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
