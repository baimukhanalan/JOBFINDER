"""Route tests for the operator «Собес» modal (backend.interviews.routes_operator),
driven through the real dashboard app via fastapi.testclient.

These hit the LIVE jobfinder_crm Postgres DB (the interviews DB layer has no mock — it
IS the contract). Every fixture uses the throwaway `test_iv_%` login/mailbox prefix and
cleans up before AND after so a crashed run never leaks rows. Skipped whole-module if
CRM_PG_DSN is unset / the DB is unreachable (mirrors test_interviews_db.py).
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

from fastapi.testclient import TestClient  # noqa: E402

from backend.dashboard_app import app  # noqa: E402
from backend.interviews import db  # noqa: E402

client = TestClient(app)

MAILBOX = "test_iv_p@takhet.com"


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


def _next_monday() -> date:
    """A safely-future Monday (>=1 week out) so booked slots are never in the past."""
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday + timedelta(days=7)


@pytest.fixture()
def seeded():
    db.ensure_schema()
    _cleanup()
    rid = db.add_responsible("test_iv_op", "h", "Оператор Тест")
    # available Mon..Fri 09:00–17:00 (540..1020 min)
    db.set_availability(rid, [
        {"dow": d, "start_min": 540, "end_min": 1020, "enabled": True}
        for d in range(5)
    ])
    yield rid
    _cleanup()


def test_grid_route_renders_cells(seeded):
    rid = seeded
    monday = _next_monday()
    r = client.get("/mail/interview/grid",
                   params={"mailbox": MAILBOX, "monday": monday.isoformat()})
    assert r.status_code == 200
    body = r.text
    # a free green cell for this responsible at an in-window hour (e.g. Monday 09:00)
    assert "iv-free" in body
    assert f"{rid}:Оператор Тест" in body
    # the 09:00 UTC start for Monday must appear as a bookable cell start
    assert f'data-start="{monday.isoformat()}T09:00:00+00:00"' in body


def test_assign_route_books(seeded):
    monday = _next_monday()
    start_iso = f"{monday.isoformat()}T09:00:00+00:00"
    rid = seeded
    r = client.post("/mail/interview/assign", data={
        "mailbox": MAILBOX,
        "responsible_id": rid,
        "start_iso": start_iso,
        "company": "Acme",
        "jobid": "job-1",
        "thread_key": "thr1",
        "source_message_hash": "hash1",
    })
    assert r.status_code == 200
    row = db.interview_for_thread(MAILBOX, "thr1")
    assert row is not None
    assert row["responsible_id"] == rid
    assert row["company"] == "Acme"


def test_assign_conflict_returns_409(seeded):
    monday = _next_monday()
    start_iso = f"{monday.isoformat()}T10:00:00+00:00"
    rid = seeded
    payload = {
        "mailbox": MAILBOX,
        "responsible_id": rid,
        "start_iso": start_iso,
        "company": "Acme",
        "jobid": "job-2",
        "thread_key": "thr2",
        "source_message_hash": "hash2",
    }
    first = client.post("/mail/interview/assign", data=payload)
    assert first.status_code == 200
    # same responsible + same start → conflict
    second = client.post("/mail/interview/assign", data={**payload, "thread_key": "thr2b"})
    assert second.status_code == 409


def test_status_route_reports_assignment(seeded):
    monday = _next_monday()
    start_iso = f"{monday.isoformat()}T11:00:00+00:00"
    rid = seeded
    client.post("/mail/interview/assign", data={
        "mailbox": MAILBOX, "responsible_id": rid, "start_iso": start_iso,
        "company": "Acme", "jobid": "job-3", "thread_key": "thr3",
        "source_message_hash": "hash3",
    })
    r = client.get("/mail/interview/status",
                   params={"mailbox": MAILBOX, "thread": "thr3"})
    assert r.status_code == 200
    data = r.json()
    assert data["assigned"] is True
    assert data["responsible"] == "Оператор Тест"
    assert data["start_ts"] is not None

    none = client.get("/mail/interview/status",
                      params={"mailbox": MAILBOX, "thread": "no-such"})
    assert none.json()["assigned"] is False
