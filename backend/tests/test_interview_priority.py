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
        {"salary_value": 100000, "deadline_ts": now + 5 * _DAY},
        {"salary_value": 200000, "deadline_ts": now + 9 * _DAY},
        {"salary_value": 0, "deadline_ts": now + 1 * _DAY},
    ]
    ordered = ip.sort_groups(rows, "salary")
    assert [r["salary_value"] for r in ordered] == [200000, 100000, 0]


def test_sort_by_urgency_soonest_first():
    now = int(time.time())
    rows = [
        {"salary_value": 100000, "deadline_ts": now + 5 * _DAY},
        {"salary_value": 200000, "deadline_ts": now + 1 * _DAY},
        {"salary_value": 50000, "deadline_ts": None},
    ]
    ordered = ip.sort_groups(rows, "urgency")
    assert ordered[0]["deadline_ts"] == now + 1 * _DAY
    assert ordered[-1]["deadline_ts"] is None  # deadline-less sorts to the bottom


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
    groups = mailcrm.candidate_groups(stage="interview", limit=8)
    ip.enrich_interview_groups(groups)
    for g in groups:
        assert "deadline_ts" in g and "deadline_days" in g and "deadline_estimated" in g
        assert g.get("direction") in ("it", "nonit", "other")
        assert isinstance(g.get("salary_value"), int)
