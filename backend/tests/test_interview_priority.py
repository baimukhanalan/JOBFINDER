"""Tests for the «Собес» priority signals: scheduling-deadline extraction + sort/split.

Pure (no DB / no network) for the extractor + partition/sort; a couple of DB-gated,
read-only checks for the whole enrich path (skipped without a CRM DSN).
"""
from __future__ import annotations

import time

import pytest

from backend.tools import interview_priority as ip

_DAY = 86400
INVITE = 1_700_000_000  # a fixed reference invite time


def _days_between(a, b):
    return round((a - b) / _DAY)


def test_within_n_days_relative():
    ts, est = ip.extract_deadline("Interview invitation",
                                  "Please schedule your interview within 3 business days.", INVITE)
    assert est is False
    assert _days_between(ts, INVITE) == 3


def test_next_n_days():
    ts, est = ip.extract_deadline("", "Book a slot in the next 7 days.", INVITE)
    assert est is False
    assert _days_between(ts, INVITE) == 7


def test_within_hours():
    ts, est = ip.extract_deadline("", "Respond within 48 hours to keep your spot.", INVITE)
    assert est is False
    assert ts == INVITE + 48 * 3600


def test_explicit_by_date():
    # a bare "March 3" resolves against the invite's year
    from datetime import datetime, timezone
    ref = datetime.fromtimestamp(INVITE, tz=timezone.utc)
    ts, est = ip.extract_deadline("", "Please complete this by March 3.", INVITE)
    assert est is False
    got = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert (got.month, got.day) == (3, 3)


def test_soonest_wins_when_multiple():
    ts, est = ip.extract_deadline(
        "", "Schedule within 10 days. Actually respond within 2 days please.", INVITE)
    assert est is False
    assert _days_between(ts, INVITE) == 2


def test_no_deadline_falls_back_estimated():
    ts, est = ip.extract_deadline("Interview", "Here is your Calendly link, pick any time.", INVITE)
    assert est is True
    assert _days_between(ts, INVITE) == ip.DEFAULT_DAYS


def test_garbage_never_raises():
    ts, est = ip.extract_deadline(None, None, 0)  # type: ignore[arg-type]
    assert isinstance(ts, int) and est is True


def test_days_left_floor_and_overdue():
    now = INVITE
    assert ip.days_left(INVITE + 3 * _DAY + 100, now) == 3
    assert ip.days_left(INVITE - _DAY, now) == -1
    assert ip.days_left(None, now) is None


def test_salary_value_picks_highest():
    assert ip.salary_value({"comp_min": 90000, "comp_max": 120000,
                            "est_total_max": 160000}) == 160000
    assert ip.salary_value({}) == 0
    assert ip.salary_value({"comp_max": "not a number"}) == 0


def test_salary_label_posted_and_estimate():
    assert ip.salary_label({"comp_min": 120000, "comp_max": 160000}) == "$120k–$160k"
    lbl = ip.salary_label({"est_total_min": 150000, "est_total_max": 150000})
    assert lbl.startswith("~")


def test_partition_it_vs_simple():
    rows = [{"direction": "it"}, {"direction": "nonit"}, {"direction": "other"}]
    it, simple = ip.partition(rows)
    assert len(it) == 1
    assert len(simple) == 2  # non-IT + unknown both go to the simple section


def test_sort_by_salary_desc_then_urgency():
    now = int(time.time())
    rows = [
        {"salary_value": 100000, "deadline_ts": now + 5 * _DAY, "deadline_days": 5},
        {"salary_value": 200000, "deadline_ts": now + 9 * _DAY, "deadline_days": 9},
        {"salary_value": 0, "deadline_ts": now + 1 * _DAY, "deadline_days": 1},
    ]
    ordered = ip.sort_groups(rows, "salary")
    assert [r["salary_value"] for r in ordered] == [200000, 100000, 0]


def test_sort_salary_expired_sinks_below_actionable():
    now = int(time.time())
    rows = [
        {"salary_value": 300000, "deadline_ts": now - 5 * _DAY, "deadline_days": -5},  # expired, top pay
        {"salary_value": 40000, "deadline_ts": now + 3 * _DAY, "deadline_days": 3},     # bookable
    ]
    ordered = ip.sort_groups(rows, "salary")
    # the bookable one leads even though the expired one pays far more — expired sinks in BOTH modes
    assert [r["deadline_days"] for r in ordered] == [3, -5]


def test_sort_by_urgency_soonest_first():
    now = int(time.time())
    rows = [
        {"salary_value": 100000, "deadline_ts": now + 5 * _DAY, "deadline_days": 5},
        {"salary_value": 200000, "deadline_ts": now + 1 * _DAY, "deadline_days": 1},
        {"salary_value": 50000, "deadline_ts": None, "deadline_days": None},
    ]
    ordered = ip.sort_groups(rows, "urgency")
    assert ordered[0]["deadline_ts"] == now + 1 * _DAY
    assert ordered[-1]["deadline_ts"] is None  # deadline-less sorts to the bottom


def test_sort_urgency_overdue_sinks_below_actionable():
    now = int(time.time())
    rows = [
        {"salary_value": 300000, "deadline_ts": now - 30 * _DAY, "deadline_days": -30},  # overdue
        {"salary_value": 10000, "deadline_ts": now + 2 * _DAY, "deadline_days": 2},       # soon
        {"salary_value": 20000, "deadline_ts": now + 6 * _DAY, "deadline_days": 6},       # later
    ]
    ordered = ip.sort_groups(rows, "urgency")
    # the two still-bookable interviews come first (soonest first), the overdue one is last —
    # even though it has the highest salary, a lapsed window isn't actionable.
    assert [r["deadline_days"] for r in ordered] == [2, 6, -30]


def test_is_expired():
    assert ip.is_expired({"deadline_days": -1}) is True
    assert ip.is_expired({"deadline_days": 0}) is False   # today = still bookable
    assert ip.is_expired({"deadline_days": 3}) is False
    assert ip.is_expired({"deadline_days": None}) is False  # unknown deadline is NOT expired


def test_role_from_email_maps_title_to_category():
    # a clear IT title classifies; a contentless invite does not (None)
    assert ip._role_from_email("Interview invitation: Senior Backend Engineer", "") is not None
    assert ip._role_from_email("Your interview is scheduled", "") is None


# ---- live DB (read-only, skipped without a CRM DSN) --------------------------------
try:
    from backend.tools import mail_db
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
    HAS_DB = True
except Exception:
    HAS_DB = False


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_enrich_interview_groups_live():
    from backend.tools import mailcrm
    groups = mailcrm.candidate_groups(stage="interview", limit=12)
    ip.enrich_interview_groups(groups)
    for g in groups:
        assert "deadline_ts" in g and "deadline_days" in g and "deadline_estimated" in g
        assert g.get("direction") in ("it", "nonit", "other")
        assert isinstance(g.get("salary_value"), int)
        # every interview candidate must get a salary label (exact comp OR category median)
        assert g.get("salary_label"), g.get("mailbox")


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_pool_excludes_expired_from_delegatable():
    from backend.interviews import pool
    bookable = pool.unallocated(limit=None)                     # default: no expired
    full = pool.unallocated(limit=None, include_expired=True)   # incl. expired
    assert all(not r.get("expired") for r in bookable)
    assert len(full) >= len(bookable)
    # facets total counts only bookable ones
    assert pool.facets().get("total", 0) == len(bookable)
