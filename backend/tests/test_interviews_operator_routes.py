"""Route tests for the operator «Собес» modal (backend.interviews.routes_operator),
driven through the real dashboard app via fastapi.testclient.

These hit the LIVE jobfinder_crm Postgres DB (the interviews DB layer has no mock — it
IS the contract). Every fixture uses the throwaway `test_iv_%` login/mailbox prefix and
cleans up before AND after so a crashed run never leaks rows. Skipped whole-module if
CRM_PG_DSN is unset / the DB is unreachable (mirrors test_interviews_db.py).
"""
from __future__ import annotations

import html
import json
import re
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
from backend.interviews import auth, db, operator_ui, service, slots  # noqa: E402

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


def _data_free_at(body: str, start_iso: str) -> list:
    """Parse the JSON `data-free` of the grid cell whose `data-start` is start_iso.
    Targets a SPECIFIC cell (not just the first free one) so the assertion is robust
    to the full 0–24 grid and to other real responsibles free in earlier cells."""
    m = re.search(re.escape(f'data-start="{start_iso}"') + r'\s+data-free="([^"]*)"', body)
    assert m, f"no free cell at {start_iso}"
    return json.loads(html.unescape(m.group(1)))


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
    # These operator routes now sit behind the dashboard's fail-closed admin gate
    # (backend.interviews.dash_auth); authenticate the client as an admin so the HTTP
    # calls reach the handlers instead of a 303 -> /login.
    admin_rid = db.add_responsible("test_iv_op_admin", auth.hash_password("x"),
                                   "Op Admin", role="admin")
    client.cookies.set(auth.COOKIE_NAME, auth.make_session(admin_rid))
    yield rid
    client.cookies.clear()
    _cleanup()


def test_grid_route_renders_cells(seeded):
    rid = seeded
    monday = _next_monday()
    r = client.get("/mail/interview/grid",
                   params={"mailbox": MAILBOX, "monday": monday.isoformat()})
    assert r.status_code == 200
    body = r.text
    # a free green cell for this responsible at an in-window LOCAL hour (Monday 09:00
    # Almaty); the cell's data-start is the corresponding UTC instant (09:00-5h)
    assert "iv-free" in body
    start_iso = slots.cell_start_utc(monday, 9).isoformat()
    assert f'data-start="{start_iso}"' in body
    # data-free is a JSON array now — parse THIS cell's list and assert we're in it
    parsed = _data_free_at(body, start_iso)
    assert {"id": rid, "name": "Оператор Тест"} in parsed


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


def test_grid_data_free_json_roundtrips_name_with_comma():
    """A responsible name with a comma/colon must survive `data-free` — it's a JSON
    array now, not a delimiter-joined string."""
    db.ensure_schema()
    _cleanup()
    try:
        rid = db.add_responsible("test_iv_comma", "h", "Ivanov, A.: Sr")
        db.set_availability(rid, [{"dow": 0, "start_min": 540, "end_min": 1020,
                                   "enabled": True}])
        monday = _next_monday()
        frag = operator_ui.grid_fragment(MAILBOX, monday)
        # target THIS user's known free cell (Monday 09:00 Almaty), unescape + JSON-parse
        parsed = _data_free_at(frag, slots.cell_start_utc(monday, 9).isoformat())
        assert {"id": rid, "name": "Ivanov, A.: Sr"} in parsed
    finally:
        _cleanup()


def test_grid_route_threads_company_jobid_and_skips_glob(seeded, monkeypatch):
    """When company/jobid are passed (prev/next-week nav), grid_fragment must NOT call
    the expensive mailbox_context glob, and the resolved values must reappear in the
    fragment's week-nav so the next hop keeps threading them."""
    def _boom(_mailbox):  # mailbox_context must not be called on the threaded path
        raise AssertionError("mailbox_context should be skipped when company/jobid given")
    monkeypatch.setattr(service, "mailbox_context", _boom)

    monday = _next_monday()
    r = client.get("/mail/interview/grid", params={
        "mailbox": MAILBOX, "monday": monday.isoformat(),
        "company": "Acme", "jobid": "job-9",
    })
    assert r.status_code == 200
    body = r.text
    assert "company=Acme" in body      # threaded into the week-nav data-ctx
    assert "jobid=job-9" in body
    assert 'value="Acme"' in body       # and into the hidden assign-form field


def test_assign_bad_start_iso_returns_400(seeded):
    rid = seeded
    r = client.post("/mail/interview/assign", data={
        "mailbox": MAILBOX, "responsible_id": rid, "start_iso": "not-a-date",
        "company": "", "jobid": "", "thread_key": "thrbad", "source_message_hash": "hb",
    })
    assert r.status_code == 400
    # and nothing was booked
    assert db.interview_for_thread(MAILBOX, "thrbad") is None
