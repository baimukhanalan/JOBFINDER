"""Pure tests for the per-interviewer weekly load calendar on /users
(backend.interviews.users_ui._week_calendar). No DB, no network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.interviews import users_ui


def _iv(dt, company="Acme", mailbox="p@takhet.com"):
    return {"responsible_id": 1, "start_ts": dt, "company": company, "mailbox": mailbox}


def test_week_calendar_counts_and_buckets():
    start = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)  # 15:00 Almaty (a Monday)
    ivs = [_iv(start), _iv(start + timedelta(days=2, hours=1), "Beta")]
    html = users_ui._week_calendar(ivs, "Asia/Almaty", None)
    assert "Собесы на неделе: <b>2</b>" in html
    assert "15:00" in html and "Acme" in html and "Beta" in html
    assert "class='u-cal'" in html


def test_week_calendar_empty():
    html = users_ui._week_calendar([], "Asia/Almaty", None)
    assert "Собесы на неделе: <b>0</b>" in html
    assert "собесов нет" in html


def test_week_calendar_shows_time_in_responsible_tz():
    # 10:00 UTC == 06:00 New York (EDT) vs 15:00 Almaty — each card is the member's own zone
    start = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    ny = users_ui._week_calendar([_iv(start)], "America/New_York", None)
    alm = users_ui._week_calendar([_iv(start)], "Asia/Almaty", None)
    assert "06:00" in ny and "06:00" not in alm
    assert "15:00" in alm


def test_week_calendar_falls_back_to_persona_localpart():
    start = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
    html = users_ui._week_calendar([_iv(start, company="", mailbox="jane.doe@takhet.com")],
                                   "Asia/Almaty", None)
    assert "jane.doe" in html


def test_week_calendar_skips_bad_start_ts():
    html = users_ui._week_calendar([{"responsible_id": 1, "start_ts": None, "company": "X"}],
                                   "UTC", None)
    assert "Собесы на неделе: <b>0</b>" in html
