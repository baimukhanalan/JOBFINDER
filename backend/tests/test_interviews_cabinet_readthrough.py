"""Admin read-through into ANY user's OWN portal from /users.

The admin can enter a MANAGER's manage-portal (`/manage?as=<id>`, already covered in
test_interviews_manager) AND an EMPLOYEE's cabinet (`/cabinet?as=<id>`, NEW). This module
covers the cabinet read-through + its ISOLATION contract: ONLY an admin's `?as` reads
through to another user; a manager/employee passing `?as=<other>` acts as HIMSELF (never a
spoof), and the per-user ownership guard keys on the TARGET being viewed.

Hits the LIVE jobfinder_crm Postgres (via mail_db's pool) — skipped if unreachable. Every
seeded row uses a `test_iv_%` login/mailbox prefix, cleaned up before AND after; assigned
rows are marked `announced=TRUE` so the live Telegram notifier never pings the owner. Run
SEQUENTIALLY with the other `test_interviews_*`.
"""
from __future__ import annotations

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
from backend.tools import mailcrm  # noqa: E402

client = TestClient(app)

_PW = "throwaway-test-pw-8823"


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


def _mark_announced():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_%'")


@pytest.fixture(autouse=True)
def _clean():
    db.ensure_schema()
    _cleanup()
    client.cookies.clear()
    yield
    _mark_announced()
    _cleanup()
    client.cookies.clear()


def _login(login: str) -> None:
    client.cookies.clear()
    r = client.post("/login", data={"login": login, "password": _PW}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.cookies.get(auth.COOKIE_NAME)
    assert cookie
    client.cookies.set(auth.COOKIE_NAME, cookie)


def _chain():
    admin = db.add_responsible("test_iv_rt_admin", auth.hash_password(_PW), "Admin RT", role="admin")
    mgrA = db.add_responsible("test_iv_rt_A", auth.hash_password(_PW), "Manager RT", role="manager")
    empE = db.add_responsible("test_iv_rt_E", auth.hash_password(_PW), "Employee RT", role="employee")
    empF = db.add_responsible("test_iv_rt_F", auth.hash_password(_PW), "Employee FF", role="employee")
    return {"admin": admin, "mgrA": mgrA, "empE": empE, "empF": empF}


def _assign(rid: int, mailbox: str, days: int = 2) -> int:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)
    return db.insert_interview(
        mailbox=mailbox, responsible_id=rid, start_ts=start,
        end_ts=start + timedelta(hours=1), company="Acme", jobid="1",
        thread_key="th", source_message_hash="srchash")


# ---- admin read-through (allow) --------------------------------------------------
def test_iv_admin_reads_through_employee_cabinet():
    ids = _chain()
    _assign(ids["empE"], "test_iv_rt_E@takhet.com")
    _login("test_iv_rt_admin")
    # admin opens employee E's cabinet — sees E's name + E's assigned interview mailbox
    r = client.get(f"/cabinet?as={ids['empE']}", follow_redirects=False)
    assert r.status_code == 200
    assert "Employee RT" in r.text
    assert "test_iv_rt_E@takhet.com" in r.text
    # the read-through banner + links carry the ?as context forward
    assert f"as={ids['empE']}" in r.text
    # availability + inbox surfaces read through too
    assert client.get(f"/cabinet/availability?as={ids['empE']}").status_code == 200
    assert client.get(f"/cabinet/inbox?as={ids['empE']}").status_code == 200


def test_iv_admin_bare_cabinet_is_self():
    """An admin hitting /cabinet with no (valid) ?as acts on their OWN cabinet, not someone else."""
    ids = _chain()
    _login("test_iv_rt_admin")
    r = client.get("/cabinet", follow_redirects=False)
    assert r.status_code == 200
    assert "Admin RT" in r.text
    assert "Employee RT" not in r.text
    # a bogus ?as target falls back to self, never errors
    r2 = client.get("/cabinet?as=99999999", follow_redirects=False)
    assert r2.status_code == 200
    assert "Admin RT" in r2.text


def test_iv_admin_read_through_thread_ownership_follows_target(monkeypatch):
    ids = _chain()
    _assign(ids["empE"], "test_iv_rt_E@takhet.com")
    _login("test_iv_rt_admin")
    # a thread on E's assigned mailbox opens under ?as=E
    monkeypatch.setattr(mail_db, "get_row",
                        lambda h: {"mailbox": "test_iv_rt_E@takhet.com", "thread_key": "t"})
    monkeypatch.setattr(mailcrm, "get_thread",
                        lambda h, *a, **k: {"subject": "Приглашение", "candidate": "Lara",
                                            "mailbox": "test_iv_rt_E@takhet.com", "messages": []})
    ok = client.get(f"/cabinet/thread?as={ids['empE']}&hash=goodhash")
    assert ok.status_code == 200 and "Приглашение" in ok.text
    # a FOREIGN mailbox (not E's) is still 404 even under the admin read-through
    monkeypatch.setattr(mail_db, "get_row",
                        lambda h: {"mailbox": "test_iv_rt_foreign@takhet.com", "thread_key": "z"})
    assert client.get(f"/cabinet/thread?as={ids['empE']}&hash=whatever").status_code == 404


# ---- isolation (deny) ------------------------------------------------------------
def test_iv_non_admin_cannot_spoof_as_in_cabinet():
    ids = _chain()
    _assign(ids["empF"], "test_iv_rt_F@takhet.com")
    # employee E tries to read employee F's cabinet via ?as → ignored, acts as HIMSELF
    _login("test_iv_rt_E")
    r = client.get(f"/cabinet?as={ids['empF']}", follow_redirects=False)
    assert r.status_code == 200
    assert "Employee RT" in r.text          # E's own name
    assert "Employee FF" not in r.text      # NOT F
    assert "test_iv_rt_F@takhet.com" not in r.text


def test_iv_manager_cannot_spoof_as_employee_cabinet(monkeypatch):
    ids = _chain()
    _assign(ids["empE"], "test_iv_rt_E@takhet.com")
    _login("test_iv_rt_A")
    # a manager passing ?as=<employee> acts as HIMSELF (manager A has no assigned mailboxes)
    r = client.get(f"/cabinet?as={ids['empE']}", follow_redirects=False)
    assert r.status_code == 200
    assert "Manager RT" in r.text
    assert "Employee RT" not in r.text
    # and E's thread is NOT reachable by the spoof — the guard keys on the ACTING (self=A)
    monkeypatch.setattr(mail_db, "get_row",
                        lambda h: {"mailbox": "test_iv_rt_E@takhet.com", "thread_key": "t"})
    assert client.get(f"/cabinet/thread?as={ids['empE']}&hash=goodhash").status_code == 404


def test_iv_non_admin_spoof_post_availability_targets_self():
    """A crafted POST with `as=<other>` from a non-admin must save to HIMSELF, never the other."""
    ids = _chain()
    _login("test_iv_rt_E")
    # E posts availability with a spoofed as=F
    r = client.post("/cabinet/availability",
                    data={"as": str(ids["empF"]), "start_0": "09:00", "end_0": "17:00"},
                    follow_redirects=False)
    assert r.status_code == 200
    # E got the window, F did NOT
    assert len(db.get_availability(ids["empE"])) == 1
    assert db.get_availability(ids["empF"]) == []


def test_iv_admin_post_availability_targets_viewed_user():
    ids = _chain()
    _login("test_iv_rt_admin")
    r = client.post("/cabinet/availability",
                    data={"as": str(ids["empE"]), "start_0": "10:00", "end_0": "18:00"},
                    follow_redirects=False)
    assert r.status_code == 200
    # the admin's edit landed on the VIEWED employee, not the admin
    assert len(db.get_availability(ids["empE"])) == 1
    assert db.get_availability(ids["admin"]) == []


# ---- /users links ----------------------------------------------------------------
def test_iv_users_page_renders_portal_links_by_role():
    ids = _chain()
    _login("test_iv_rt_admin")
    r = client.get("/users")
    assert r.status_code == 200
    # a manager gets a /manage?as link; an employee gets a /cabinet?as link
    assert f"/manage?as={ids['mgrA']}" in r.text
    assert f"/cabinet?as={ids['empE']}" in r.text
