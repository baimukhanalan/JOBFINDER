"""Manager («Управляющий») tier — the middle role between admin and employee.

Covers the DB layer (manager_id columns, allocation, delegated assignment, per-team load),
the fail-closed auth routing (a manager is confined to /manage + /cabinet), and the
assignment ISOLATION contract (a manager can only assign interviews from HIS pool, only to
himself or HIS subordinates; another manager can't see or touch them).

Hits the LIVE jobfinder_crm Postgres (via mail_db's pool) — skipped if the DB is
unreachable. Every seeded row uses a `test_iv_%` login/mailbox prefix and a fixture cleans
them up before AND after, so a crash never leaks rows. Assigned test rows are marked
`announced=TRUE` immediately so the live Telegram notifier daemon never pings the owner
about throwaway test interviews. Run SEQUENTIALLY with the other `test_interviews_*`.
"""
from __future__ import annotations

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

_PW = "throwaway-test-pw-4471"


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


def _mark_announced():
    # never let the live notifier daemon ping the owner about throwaway test interviews
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
    """admin → manager A (+ subordinate S) and an unrelated manager B + plain employee E."""
    admin = db.add_responsible("test_iv_m_admin", auth.hash_password(_PW), "Admin", role="admin")
    mgrA = db.add_responsible("test_iv_m_A", auth.hash_password(_PW), "Manager A", role="manager")
    mgrB = db.add_responsible("test_iv_m_B", auth.hash_password(_PW), "Manager B", role="manager")
    subS = db.add_responsible("test_iv_m_S", auth.hash_password(_PW), "Sub S",
                              role="employee", manager_id=mgrA)
    empE = db.add_responsible("test_iv_m_E", auth.hash_password(_PW), "Emp E", role="employee")
    return {"admin": admin, "mgrA": mgrA, "mgrB": mgrB, "subS": subS, "empE": empE}


# ---- DB layer --------------------------------------------------------------------
def test_iv_manager_id_roundtrip_and_subordinates():
    ids = _chain()
    # the subordinate carries its manager; the manager's team lists exactly that subordinate
    assert db.get_responsible(ids["subS"])["manager_id"] == ids["mgrA"]
    subs = db.subordinates(ids["mgrA"])
    assert {s["id"] for s in subs} == {ids["subS"]}
    # a plain employee (no manager) is nobody's subordinate
    assert db.subordinates(ids["mgrB"]) == []
    assert db.get_responsible(ids["empE"])["manager_id"] is None
    # set/clear a manager
    db.set_manager(ids["empE"], ids["mgrB"])
    assert {s["id"] for s in db.subordinates(ids["mgrB"])} == {ids["empE"]}
    db.set_manager(ids["empE"], None)
    assert db.subordinates(ids["mgrB"]) == []
    # list_managers finds both managers, not the employees/admin
    mgr_ids = {m["id"] for m in db.list_managers()}
    assert ids["mgrA"] in mgr_ids and ids["mgrB"] in mgr_ids
    assert ids["subS"] not in mgr_ids and ids["admin"] not in mgr_ids


def test_iv_allocate_assign_and_load():
    ids = _chain()
    # allocate two pool interviews to manager A
    i1 = db.allocate_interview("test_iv_m_p1@x.com", ids["mgrA"], subject="Interview 1")
    i2 = db.allocate_interview("test_iv_m_p2@x.com", ids["mgrA"], subject="Interview 2")
    pool_rows = db.manager_interviews(ids["mgrA"])
    assert {r["id"] for r in pool_rows} == {i1, i2}
    assert all(r["responsible_id"] is None and r["status"] == "pool" for r in pool_rows)
    # they count as handled (out of the free pool)
    handled = db.handled_pool_mailboxes()
    assert {"test_iv_m_p1@x.com", "test_iv_m_p2@x.com"} <= handled

    # manager A assigns one to subordinate S → visibility for S, load reflects it
    db.manager_assign_interview(i1, ids["subS"])
    _mark_announced()
    row = db.interview_by_id(i1)
    assert row["responsible_id"] == ids["subS"] and row["status"] == "assigned"
    assert row["manager_id"] == ids["mgrA"]           # allocation is preserved on assign
    assert "test_iv_m_p1@x.com" in db.assigned_mailboxes(ids["subS"])
    assert db.assigned_load([ids["subS"]]).get(ids["subS"]) == 1

    # manager B sees NOTHING of A's pool
    assert db.manager_interviews(ids["mgrB"]) == []

    # unassign returns it to the pool (S loses visibility)
    db.manager_unassign_interview(i1)
    row = db.interview_by_id(i1)
    assert row["responsible_id"] is None and row["status"] == "pool"
    assert "test_iv_m_p1@x.com" not in db.assigned_mailboxes(ids["subS"])

    # deallocate an unassigned pool row removes it entirely (back to the free pool)
    db.deallocate_interview(i2)
    assert i2 not in {r["id"] for r in db.manager_interviews(ids["mgrA"])}
    assert "test_iv_m_p2@x.com" not in db.handled_pool_mailboxes()


# ---- auth routing (fail-closed) --------------------------------------------------
def test_iv_manager_confined_to_manage_and_cabinet():
    ids = _chain()
    # a manager login lands on /manage
    r = client.post("/login", data={"login": "test_iv_m_A", "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/manage"
    cookie = r.cookies.get(auth.COOKIE_NAME)
    client.cookies.set(auth.COOKIE_NAME, cookie)

    # /manage reaches its handler (200), /cabinet is allowed too
    assert client.get("/manage", follow_redirects=False).status_code == 200
    assert client.get("/cabinet", follow_redirects=False).status_code == 200
    # every admin surface is bounced to /manage (never leaks the PII dashboard)
    for path in ("/users", "/mail/candidates", "/catalog", "/stats"):
        rr = client.get(path, follow_redirects=False)
        assert rr.status_code == 303 and rr.headers["location"] == "/manage", path


def test_iv_employee_still_confined_to_cabinet():
    _chain()
    r = client.post("/login", data={"login": "test_iv_m_E", "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet"
    client.cookies.set(auth.COOKIE_NAME, r.cookies.get(auth.COOKIE_NAME))
    # an employee cannot reach the manager portal — bounced to /cabinet
    rr = client.get("/manage", follow_redirects=False)
    assert rr.status_code == 303 and rr.headers["location"] == "/cabinet"


# ---- assignment isolation over HTTP ----------------------------------------------
def test_iv_manager_assign_route_and_isolation():
    ids = _chain()
    iid = db.allocate_interview("test_iv_m_h1@x.com", ids["mgrA"], subject="HTTP interview")

    # manager A assigns to subordinate S via the route → OK, row updated
    _login("test_iv_m_A")
    r = client.post("/manage/assign",
                    data={"iid": iid, "responsible_id": ids["subS"]}, follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    assert db.interview_by_id(iid)["responsible_id"] == ids["subS"]

    # manager A CANNOT assign to a non-subordinate (the plain employee E) — rejected, unchanged
    r = client.post("/manage/assign",
                    data={"iid": iid, "responsible_id": ids["empE"]}, follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    assert db.interview_by_id(iid)["responsible_id"] == ids["subS"]  # still S, not E

    # manager B CANNOT touch A's interview (not his pool) — rejected, unchanged
    _login("test_iv_m_B")
    r = client.post("/manage/assign",
                    data={"iid": iid, "responsible_id": ids["mgrB"]}, follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    assert db.interview_by_id(iid)["responsible_id"] == ids["subS"]  # untouched by B

    # B's own portal shows none of A's interviews
    r = client.get("/manage", follow_redirects=False)
    assert r.status_code == 200
    assert "test_iv_m_h1" not in r.text


def test_iv_admin_read_through_portal():
    ids = _chain()
    db.allocate_interview("test_iv_m_rt@x.com", ids["mgrA"], subject="Read-through interview")
    _login("test_iv_m_admin")
    # admin can view manager A's portal read-through
    r = client.get(f"/manage?as={ids['mgrA']}", follow_redirects=False)
    assert r.status_code == 200
    assert "Manager A" in r.text
    assert "test_iv_m_rt" in r.text
    # admin with no target is sent to the roster
    r = client.get("/manage", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/users")
