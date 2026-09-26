"""End-to-end for the redesigned user portals (2026-09-26): the shared LEFT-MENU shell, the
home week-calendar + collapsible actionable list, the «Инструкции» section, the self-schedule +
mark-done controls, and the FIX for the «События найма» admin-rail nav leak.

Hits the LIVE jobfinder_crm Postgres (skipped if unreachable). Every seeded row uses a
`test_iv_%` prefix, cleaned up before + after; assigned rows are forced `announced=TRUE` on
teardown so the live Telegram notifier never pings the owner. Run SEQUENTIALLY with the other
`test_interviews_*` (shared live DB)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from backend.dashboard_app import app  # noqa: E402
from backend.interviews import auth, db  # noqa: E402

client = TestClient(app)
_PW = "throwaway-test-pw-7741"
_MB = "test_iv_pu_cand@takhet.com"


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")
        cur.execute("DELETE FROM mail_index WHERE mailbox LIKE 'test_iv_%'")


def _mark_announced():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_%'")


def _retry(fn, tries: int = 6):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.6)


@pytest.fixture(autouse=True)
def _clean():
    _retry(db.ensure_schema)
    _retry(_cleanup)
    client.cookies.clear()
    yield
    _retry(_mark_announced)
    _retry(_cleanup)
    client.cookies.clear()


def _login(login: str) -> None:
    def _do():
        client.cookies.clear()
        r = client.post("/login", data={"login": login, "password": _PW}, follow_redirects=False)
        assert r.status_code == 303
        client.cookies.set(auth.COOKIE_NAME, r.cookies.get(auth.COOKIE_NAME))
    _retry(_do)


def _stable_get(url: str, tries: int = 20):
    r = None
    for _ in range(tries):
        r = client.get(url, follow_redirects=False)
        transient = (r.status_code in (302, 303, 307)
                     or (r.status_code == 200 and 'action="/login"' in r.text))
        if not transient:
            return r
        time.sleep(1.0)
    return r


def _post(url: str, data: dict, tries: int = 20):
    """POST that rides out the transient fail-closed-to-/login auth artifact (303→/login);
    the real handler 303s to /cabinet."""
    r = None
    for _ in range(tries):
        r = client.post(url, data=data, follow_redirects=False)
        loc = r.headers.get("location", "")
        if not (r.status_code in (302, 303, 307) and loc.endswith("/login")):
            return r
        time.sleep(1.0)
    return r


def _seed_mail(mailbox: str) -> str:
    ph = f"hash_{mailbox}".replace("@", "_")[:60]
    _retry(lambda: mail_db.upsert_message(
        mailbox=mailbox, candidate=mailbox.split("@")[0], candidate_id=None,
        path=f"/tmp/{ph}", path_hash=ph, from_name="Recruiter", from_email="rec@corp.com",
        subject="Interview invitation", snippet="body", kind="interview", thread_key="t1",
        has_att=False, outbound=False, date_ts=int(datetime.now(timezone.utc).timestamp()),
        seen=False, booking_url=None, booking_provider=None))
    return ph


def _seed():
    rid = _retry(lambda: db.add_responsible("test_iv_pu_E", auth.hash_password(_PW),
                                            "PU Employee", role="employee"))
    ph = _seed_mail(_MB)
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    iid = _retry(lambda: db.insert_interview(
        mailbox=_MB, responsible_id=rid, start_ts=start, end_ts=start + timedelta(hours=1),
        company="Acme", jobid="1", thread_key="t1", source_message_hash=ph))
    _retry(_mark_announced)
    return rid, iid


def test_home_has_left_menu_week_calendar_and_actionable_row():
    rid, iid = _seed()
    _login("test_iv_pu_E")
    r = _stable_get("/cabinet")
    assert r.status_code == 200
    t = r.text
    assert 'class="sidebar"' in t and ">Главная<" in t and ">Инструкции<" in t
    assert "wk-grid" in t                       # the week calendar
    assert 'class="hv-row' in t                 # the actionable interview row
    assert "/cabinet/self_schedule" in t and "/cabinet/mark_done" in t


def test_guide_is_role_specific():
    _seed()
    _login("test_iv_pu_E")
    r = _stable_get("/cabinet/guide")
    assert r.status_code == 200
    assert "Как проводить собеседования" in r.text          # interviewer section
    assert "Как распределять" not in r.text                 # NOT a manager → no manager section


def test_hiring_events_has_no_admin_nav_leak_for_non_admin():
    _seed()
    _login("test_iv_pu_E")
    r = _stable_get("/hiring-events")
    assert r.status_code == 200
    t = r.text
    assert 'class="sidebar"' in t and ">Главная<" in t      # the USER shell
    # the admin sections must NOT be reachable from here
    for admin in ('href="/stats"', 'href="/users"', 'href="/catalog"', 'href="/health"',
                  'href="/mail/candidates"'):
        assert admin not in t, f"admin nav leaked: {admin}"


def test_mark_done_and_self_schedule_roundtrip():
    rid, iid = _seed()
    _login("test_iv_pu_E")
    # mark done → done_at set, собес shows under «Проведённые»
    r = _post("/cabinet/mark_done", {"iid": str(iid), "done": "1"})
    assert r.status_code == 303
    row = _retry(lambda: db.interview_by_id(iid))
    assert row and row.get("done_at") is not None
    home = _stable_get("/cabinet").text
    assert "Проведённые" in home
    # un-mark → done_at cleared
    r = _post("/cabinet/mark_done", {"iid": str(iid), "done": "0"})
    assert r.status_code == 303
    assert _retry(lambda: db.interview_by_id(iid)).get("done_at") is None
    # self-schedule → start_ts updated to the chosen wall-clock (in the user's tz)
    r = _post("/cabinet/self_schedule", {"iid": str(iid), "start_local": "2030-01-15T14:30"})
    assert r.status_code == 303
    row = _retry(lambda: db.interview_by_id(iid))
    assert row.get("start_ts") is not None and row["start_ts"].year == 2030


def test_self_schedule_rejects_a_foreign_interview():
    """A user can only set the time of THEIR OWN собес — a POST for someone else's iid is a no-op."""
    rid, iid = _seed()
    other = _retry(lambda: db.add_responsible("test_iv_pu_O", auth.hash_password(_PW),
                                              "Other", role="employee"))
    _login("test_iv_pu_O")           # a DIFFERENT user
    before = _retry(lambda: db.interview_by_id(iid)).get("start_ts")
    r = _post("/cabinet/self_schedule", {"iid": str(iid), "start_local": "2031-02-02T09:00"})
    assert r.status_code == 303
    after = _retry(lambda: db.interview_by_id(iid)).get("start_ts")
    assert before == after           # unchanged — not their собес
