"""Scoped candidate inbox + personal calendar in the interviewer/manager cabinet.

Covers the NEW cabinet surfaces layered on the interview scheduler:
  * `mail_db.candidate_groups(mailboxes=[...])` restricts to those mailboxes, and
    `mailboxes=None` is byte-identical to before (the admin «Кандидаты» tab must not change);
  * `GET /cabinet/candidates` renders ONLY the acting user's собес candidates + its search;
  * the thread/message fragments enforce the ownership guard (a mailbox OUTSIDE the scope 404s);
  * `GET /cabinet/calendar` renders an assigned interview.

Hits the LIVE jobfinder_crm Postgres (via mail_db's pool) — skipped if unreachable. Every seeded
row uses a `test_iv_%` login/mailbox prefix, cleaned up before AND after; assigned rows are marked
`announced=TRUE` so the live Telegram notifier never pings the owner. Run SEQUENTIALLY with the
other `test_interviews_*`.
"""
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

_PW = "throwaway-test-pw-8823"
_MB_E = "test_iv_ci_e@takhet.com"      # employee E's assigned candidate
_MB_FOREIGN = "test_iv_ci_foreign@takhet.com"  # a candidate NOT in E's scope


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")
        cur.execute("DELETE FROM mail_index WHERE mailbox LIKE 'test_iv_%'")


def _mark_announced():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_%'")


def _retry(fn, tries: int = 6):
    """The live indexer/dash contend for the mail_index/iv_interviews locks, so a schema/cleanup
    DDL can transiently deadlock or lock-timeout (the documented DB-lock gotcha). Retry briefly."""
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
        cookie = r.cookies.get(auth.COOKIE_NAME)
        assert cookie
        client.cookies.set(auth.COOKIE_NAME, cookie)
    _retry(_do)


def _stable_get(url: str, tries: int = 20):
    """GET, retrying past a TRANSIENT auth artifact. The session cookie is a stateless signed rid,
    but the auth middleware re-reads the responsible from the DB every request; under live-service
    DB contention that read can time out and the request fails closed to /login (a 303, or the login
    page at 200) — an artifact, not the route's real answer. Retry (patiently, to ride out a busy
    indexer/dash window) until we get a genuine cabinet response."""
    r = None
    for i in range(tries):
        r = client.get(url, follow_redirects=False)
        transient = (r.status_code in (302, 303, 307)
                     or (r.status_code == 200 and 'action="/login"' in r.text))
        if not transient:
            return r
        time.sleep(1.0)
    return r


def _add_emp() -> int:
    return _retry(lambda: db.add_responsible(
        "test_iv_ci_E", auth.hash_password(_PW), "Employee CI", role="employee"))


def _seed_mail(mailbox: str, subject: str, kind: str = "interview") -> str:
    """One mail_index row for a candidate mailbox (enough for candidate_groups to return it)."""
    ph = f"hash_{mailbox}_{subject}".replace("@", "_").replace(" ", "_")[:60]
    _retry(lambda: mail_db.upsert_message(
        mailbox=mailbox, candidate=mailbox.split("@")[0], candidate_id=None,
        path=f"/tmp/{ph}", path_hash=ph, from_name="Recruiter", from_email="rec@corp.com",
        subject=subject, snippet="body", kind=kind, thread_key="t1", has_att=False,
        outbound=False, date_ts=int(datetime.now(timezone.utc).timestamp()), seen=False,
        booking_url=None, booking_provider=None))
    return ph


def _assign(rid: int, mailbox: str, days: int = 2) -> int:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)
    return _retry(lambda: db.insert_interview(
        mailbox=mailbox, responsible_id=rid, start_ts=start,
        end_ts=start + timedelta(hours=1), company="Acme", jobid="1",
        thread_key="t1", source_message_hash="srchash"))


# ---- mail_db.candidate_groups mailboxes param -----------------------------------
def test_iv_candidate_groups_mailboxes_scopes_and_none_unchanged():
    _seed_mail(_MB_E, "Приглашение E")
    _seed_mail(_MB_FOREIGN, "Приглашение F")

    scoped = mail_db.candidate_groups(mailboxes=[_MB_E])
    boxes = {g["mailbox"] for g in scoped}
    assert boxes == {_MB_E}                       # ONLY the scoped mailbox

    # mailboxes=None (the default) is unchanged — both seeded mailboxes are present in the full set
    allrows = mail_db.candidate_groups(mailboxes=None, limit=500)
    allboxes = {g["mailbox"] for g in allrows}
    assert _MB_E in allboxes and _MB_FOREIGN in allboxes

    # an empty scope ⇒ no rows (a responsible with no assigned mailboxes)
    assert mail_db.candidate_groups(mailboxes=[]) == []


# ---- scoped /cabinet/candidates -------------------------------------------------
def test_iv_cabinet_candidates_shows_only_scoped_candidate():
    emp = _add_emp()
    _assign(emp, _MB_E)
    _seed_mail(_MB_E, "Приглашение E")
    _seed_mail(_MB_FOREIGN, "Приглашение F")
    _login("test_iv_ci_E")

    r = _stable_get("/cabinet/candidates")
    assert r.status_code == 200
    assert _MB_E in r.text            # E's assigned candidate appears
    assert _MB_FOREIGN not in r.text  # a candidate outside E's scope does NOT


def test_iv_cabinet_candidate_thread_ownership_guard():
    emp = _add_emp()
    _assign(emp, _MB_E)
    _seed_mail(_MB_E, "Приглашение E")
    _seed_mail(_MB_FOREIGN, "Приглашение F")
    _login("test_iv_ci_E")

    # the assigned candidate's thread opens
    ok = _stable_get(f"/cabinet/candidates/thread?mailbox={_MB_E}")
    assert ok.status_code == 200

    # a candidate OUTSIDE the scope is 404, never leaks
    denied = _stable_get(f"/cabinet/candidates/thread?mailbox={_MB_FOREIGN}")
    assert denied.status_code == 404

    # a message row whose mailbox is out of scope is 404 too
    ph = _seed_mail(_MB_FOREIGN, "Секрет")
    assert _stable_get(f"/cabinet/candidates/message?id={ph}").status_code == 404


# ---- personal calendar ----------------------------------------------------------
def test_iv_cabinet_calendar_renders_assigned_interview():
    emp = _add_emp()
    _assign(emp, _MB_E)
    _login("test_iv_ci_E")

    r = _stable_get("/cabinet/calendar")
    assert r.status_code == 200
    assert "Мой календарь" in r.text
    assert "Acme" in r.text            # the assigned interview's company
    assert _MB_E in r.text
