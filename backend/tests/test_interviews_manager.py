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
    # allocate two interviews to manager A — the AUTO-OWN model makes HIM the attendee at once
    i1 = db.allocate_interview("test_iv_m_p1@x.com", ids["mgrA"], subject="Interview 1")
    i2 = db.allocate_interview("test_iv_m_p2@x.com", ids["mgrA"], subject="Interview 2")
    _mark_announced()
    rows = db.manager_interviews(ids["mgrA"])
    assert {r["id"] for r in rows} == {i1, i2}
    # each lands ASSIGNED to the manager himself (responsible_id=manager_id), NOT a null pool
    assert all(r["responsible_id"] == ids["mgrA"] and r["status"] == "assigned" for r in rows)
    # they count as handled (out of the free pool)
    handled = db.handled_pool_mailboxes()
    assert {"test_iv_m_p1@x.com", "test_iv_m_p2@x.com"} <= handled

    # manager A hands one DOWN to subordinate S → visibility for S, load reflects it
    db.manager_assign_interview(i1, ids["subS"])
    _mark_announced()
    row = db.interview_by_id(i1)
    assert row["responsible_id"] == ids["subS"] and row["status"] == "assigned"
    assert row["manager_id"] == ids["mgrA"]           # allocation is preserved on hand-down
    assert "test_iv_m_p1@x.com" in db.assigned_mailboxes(ids["subS"])
    assert db.assigned_load([ids["subS"]]).get(ids["subS"]) == 1

    # manager B sees NOTHING of A's set
    assert db.manager_interviews(ids["mgrB"]) == []

    # unassign pulls it back to the MANAGER himself (not a null pool)
    db.manager_unassign_interview(i1)
    _mark_announced()
    row = db.interview_by_id(i1)
    assert row["responsible_id"] == ids["mgrA"] and row["status"] == "assigned"
    assert "test_iv_m_p1@x.com" not in db.assigned_mailboxes(ids["subS"])
    assert "test_iv_m_p1@x.com" in db.assigned_mailboxes(ids["mgrA"])   # back with the manager

    # admin reclaim pulls a собес fully back to the global free pool (deletes the row)
    freed = db.reclaim_interview(i2)
    assert freed == "test_iv_m_p2@x.com"
    assert i2 not in {r["id"] for r in db.manager_interviews(ids["mgrA"])}
    assert "test_iv_m_p2@x.com" not in db.handled_pool_mailboxes()


def test_iv_allocated_is_owned_then_handed_down():
    # M1 (new model): an allocated собес is IMMEDIATELY the manager's assignment (auto-own);
    # handing it down re-points the assignment at the subordinate. Either way it badges «Назначено».
    ids = _chain()
    mb = "test_iv_m_pa@x.com"
    iid = db.allocate_interview(mb, ids["mgrA"], subject="Interview P")
    _mark_announced()
    a = db.assignments_for_mailboxes([mb])
    assert mb in a and a[mb]["responsible_id"] == ids["mgrA"]   # auto-owned by the manager
    db.manager_assign_interview(iid, ids["subS"])
    _mark_announced()
    a = db.assignments_for_mailboxes([mb])
    assert mb in a and a[mb]["responsible_id"] == ids["subS"]   # handed down → the subordinate


def test_iv_manage_assign_availability_gate():
    # M2: a timed manager-assign must be gated on availability/overlap (not just the exact-start
    # partial-unique) — an interviewer with no window can't be booked; one with a window can.
    ids = _chain()
    mb = "test_iv_m_av@x.com"
    iid = db.allocate_interview(mb, ids["mgrA"], subject="Interview AV")
    _login("test_iv_m_A")
    start_local = "2026-12-15T10:00"
    client.post("/manage/assign", data={"iid": iid, "responsible_id": ids["subS"],
                                        "start_local": start_local}, follow_redirects=False)
    _mark_announced()
    row = db.interview_by_id(iid)
    # rejected (no availability): the собес stays with the MANAGER himself (auto-own), never a null pool
    assert row["responsible_id"] == ids["mgrA"] and row["status"] == "assigned"
    # give S a 24h window every weekday → the same slot is now bookable
    db.set_availability(ids["subS"], [{"dow": d, "start_min": 0, "end_min": 0, "enabled": True}
                                      for d in range(7)])
    client.post("/manage/assign", data={"iid": iid, "responsible_id": ids["subS"],
                                        "start_local": start_local}, follow_redirects=False)
    _mark_announced()
    row = db.interview_by_id(iid)
    assert row["responsible_id"] == ids["subS"] and row["status"] == "assigned"


def test_iv_manage_unassign_rejects_cancelled():
    # M3: unassign must reject a cancelled row (mirror manage_assign) — no resurrection to pool.
    ids = _chain()
    mb = "test_iv_m_c@x.com"
    iid = db.allocate_interview(mb, ids["mgrA"], subject="Interview C")
    db.manager_assign_interview(iid, ids["subS"])
    _mark_announced()
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET status='cancelled' WHERE id=%s", (iid,))
    _login("test_iv_m_A")
    client.post("/manage/unassign", data={"iid": iid}, follow_redirects=False)
    assert db.interview_by_id(iid)["status"] == "cancelled"   # NOT resurrected to pool


# ---- auth routing (fail-closed) --------------------------------------------------
def test_iv_manager_confined_to_manage_and_cabinet():
    ids = _chain()
    # a manager login lands on /cabinet (Главная) — the portal starts on the user home (2026-09-26)
    r = client.post("/login", data={"login": "test_iv_m_A", "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet"
    cookie = r.cookies.get(auth.COOKIE_NAME)
    client.cookies.set(auth.COOKIE_NAME, cookie)

    # /manage reaches its handler (200), /cabinet is allowed too
    assert client.get("/manage", follow_redirects=False).status_code == 200
    assert client.get("/cabinet", follow_redirects=False).status_code == 200
    # every admin surface is bounced to /cabinet (never leaks the PII dashboard)
    for path in ("/users", "/mail/candidates", "/catalog", "/stats"):
        rr = client.get(path, follow_redirects=False)
        assert rr.status_code == 303 and rr.headers["location"] == "/cabinet", path


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


# ---- multi-role -----------------------------------------------------------------
def test_iv_multi_role_db_roundtrip():
    rid = db.add_responsible("test_iv_mr_am", "h", "AdminMgr", roles=["admin", "manager"])
    u = db.get_responsible(rid)
    assert set(db.roles_of(u)) == {"admin", "manager"}
    assert u["role"] == "admin"                       # legacy column mirrors the PRIMARY
    assert db.has_role(u, "admin") and db.has_role(u, "manager")
    # a user holding manager (even alongside admin) is listed as a manager
    assert rid in {m["id"] for m in db.list_managers(active_only=False)}
    # set_roles replaces the set + re-mirrors primary; normalisation drops junk/dupes
    db.set_roles(rid, ["employee", "manager", "manager", "bogus"])
    u = db.get_responsible(rid)
    assert set(db.roles_of(u)) == {"employee", "manager"} and u["role"] == "manager"
    # empty → never role-less
    db.set_roles(rid, [])
    assert db.roles_of(db.get_responsible(rid)) == ["employee"]


def test_iv_multi_role_access_union():
    # [admin, manager]: full admin access AND his own /manage portal
    am = db.add_responsible("test_iv_mr_A", auth.hash_password(_PW), "AM", roles=["admin", "manager"])
    r = client.post("/login", data={"login": "test_iv_mr_A", "password": _PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"      # home = highest surface
    client.cookies.set(auth.COOKIE_NAME, r.cookies.get(auth.COOKIE_NAME))
    assert client.get("/users", follow_redirects=False).status_code == 200          # admin surface
    assert client.get("/manage", follow_redirects=False).status_code == 200          # own portal
    assert client.get("/mail/candidates", follow_redirects=False).status_code == 200  # admin surface

    # [manager, employee]: /manage AND /cabinet, but NOT admin surfaces
    db.add_responsible("test_iv_mr_ME", auth.hash_password(_PW), "ME", roles=["manager", "employee"])
    r = client.post("/login", data={"login": "test_iv_mr_ME", "password": _PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cabinet"   # lands on Главная
    client.cookies.set(auth.COOKIE_NAME, r.cookies.get(auth.COOKIE_NAME))
    assert client.get("/manage", follow_redirects=False).status_code == 200
    assert client.get("/cabinet", follow_redirects=False).status_code == 200
    rr = client.get("/users", follow_redirects=False)
    assert rr.status_code == 303 and rr.headers["location"] == "/cabinet"   # bounced off admin → Главная


def test_iv_role_edit_from_list_changes_access_live():
    admin = db.add_responsible("test_iv_re_adm", auth.hash_password(_PW), "Adm", role="admin")
    emp = db.add_responsible("test_iv_re_emp", auth.hash_password(_PW), "Emp", role="employee")
    # before: the employee is bounced off /manage
    r = client.post("/login", data={"login": "test_iv_re_emp", "password": _PW}, follow_redirects=False)
    emp_cookie = r.cookies.get(auth.COOKIE_NAME)
    client.cookies.set(auth.COOKIE_NAME, emp_cookie)
    assert client.get("/manage", follow_redirects=False).headers["location"] == "/cabinet"

    # admin promotes them to [manager, employee] via the INLINE list editor
    _login("test_iv_re_adm")
    r = client.post(f"/users/{emp}/roles",
                    data={"role": ["manager", "employee"], "from_list": "1"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert set(db.roles_of(db.get_responsible(emp))) == {"manager", "employee"}

    # the SAME employee session now reaches /manage (access changed live, no re-login)
    client.cookies.set(auth.COOKIE_NAME, emp_cookie)
    assert client.get("/manage", follow_redirects=False).status_code == 200


# ---- delete any user (incl. deactivated / with history) --------------------------
def test_iv_delete_returns_interviews_to_pool():
    ids = _chain()
    iid = db.allocate_interview("test_iv_m_del@x.com", ids["mgrA"], subject="Del interview")
    db.manager_assign_interview(iid, ids["subS"])
    _mark_announced()
    # deactivate the subordinate, then hard-delete them as admin
    db.set_active(ids["subS"], False)
    _login("test_iv_m_admin")
    r = client.post(f"/users/{ids['subS']}/delete", follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    # the subordinate is gone; their interview is back in manager A's pool, allocation kept
    assert db.get_responsible(ids["subS"]) is None
    row = db.interview_by_id(iid)
    assert row is not None and row["responsible_id"] is None
    assert row["status"] == "pool" and row["manager_id"] == ids["mgrA"]


def test_iv_delete_manager_detaches_subordinates_and_frees_pool():
    ids = _chain()
    iid = db.allocate_interview("test_iv_m_delm@x.com", ids["mgrA"], subject="Mgr del")
    _login("test_iv_m_admin")
    r = client.post(f"/users/{ids['mgrA']}/delete", follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    assert db.get_responsible(ids["mgrA"]) is None            # manager removed
    assert db.get_responsible(ids["subS"])["manager_id"] is None  # subordinate detached
    assert db.interview_by_id(iid) is None                    # delegation row freed
    assert "test_iv_m_delm@x.com" not in db.handled_pool_mailboxes()  # back to free pool


# ---- pool enrichment: gender + direction filters --------------------------------
def test_iv_pool_direction_and_match():
    from backend.interviews import pool
    assert pool.direction_of("Engineering") == "it"
    assert pool.direction_of("Data & ML") == "it"
    assert pool.direction_of("Customer Support & Success") == "nonit"
    assert pool.direction_of("Other") == "other"
    assert pool.direction_of(None) == "other"
    row = {"mailbox": "jane.doe1@takhet.com", "candidate": "Jane Doe",
           "sex": "female", "direction": "it"}
    assert pool._match(row, None, None, None)
    assert pool._match(row, "jane", "female", "it")       # email + gender + direction all hit
    assert not pool._match(row, None, "male", None)        # wrong gender
    assert not pool._match(row, None, None, "nonit")       # wrong direction
    assert not pool._match(row, "zzz", None, None)         # email/name miss


def _it_nonit_jobids():
    with mail_db._cur() as cur:
        cur.execute("SELECT id FROM job_catalog WHERE role_category='Engineering' LIMIT 1")
        r = cur.fetchone()
        it_id = str(r["id"]) if r else None
        cur.execute("SELECT id FROM job_catalog WHERE role_category IN "
                    "('Operations','Finance & Accounting','Customer Support & Success') LIMIT 1")
        r = cur.fetchone()
        nonit_id = str(r["id"]) if r else None
    return it_id, nonit_id


def test_iv_enrich_iv_rows_direction_from_jobid():
    from backend.interviews import pool
    it_id, nonit_id = _it_nonit_jobids()
    if not it_id or not nonit_id:
        pytest.skip("no categorized jobs in job_catalog")
    rows = [{"mailbox": "test_iv_e_a@x.com", "jobid": it_id},
            {"mailbox": "test_iv_e_b@x.com", "jobid": nonit_id},
            {"mailbox": "test_iv_e_c@x.com", "jobid": None}]
    pool.enrich_iv_rows(rows)
    assert rows[0]["direction"] == "it"
    assert rows[1]["direction"] == "nonit"
    assert rows[2]["direction"] == "other"     # no jobid → uncategorized, never dropped


def test_iv_manager_distribute_to_with_direction_and_isolation():
    it_id, _ = _it_nonit_jobids()
    if not it_id:
        pytest.skip("no Engineering job in job_catalog")
    ids = _chain()
    i1 = db.allocate_interview("test_iv_m_d1@x.com", ids["mgrA"], jobid=it_id, subject="s1")
    i2 = db.allocate_interview("test_iv_m_d2@x.com", ids["mgrA"], jobid=it_id, subject="s2")
    i3 = db.allocate_interview("test_iv_m_d3@x.com", ids["mgrA"], jobid="", subject="s3")  # other

    _login("test_iv_m_A")
    # give 2 IT interviews to subordinate S
    r = client.post("/manage/distribute_to",
                    data={"member_id": ids["subS"], "count": 2, "direction": "it"},
                    follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    got = [db.interview_by_id(i)["responsible_id"] for i in (i1, i2, i3)]
    assert got.count(ids["subS"]) == 2                       # the two IT ones handed down
    # the 'other' one wasn't matched → stays with the MANAGER himself (auto-own), not handed down
    assert db.interview_by_id(i3)["responsible_id"] == ids["mgrA"]

    # ISOLATION: a manager cannot distribute to a non-subordinate (the plain employee E)
    r = client.post("/manage/distribute_to",
                    data={"member_id": ids["empE"], "count": 1}, follow_redirects=False)
    _mark_announced()
    assert not db.assigned_load([ids["empE"]]).get(ids["empE"])


def test_iv_manager_portal_two_sections():
    ids = _chain()
    db.allocate_interview("test_iv_m_s1@x.com", ids["mgrA"], subject="mine")   # auto-owned
    i_team = db.allocate_interview("test_iv_m_s2@x.com", ids["mgrA"], subject="teammate")
    db.manager_assign_interview(i_team, ids["subS"])   # handed DOWN to a subordinate
    _mark_announced()
    _login("test_iv_m_A")
    r = client.get("/manage", follow_redirects=False)
    assert r.status_code == 200
    # the two required sections of the auto-own model are present + separated
    assert "Собеседования за вами" in r.text and "Роздано команде" in r.text
    assert "test_iv_m_s1" in r.text                    # my own (not distributed) shows
    assert "test_iv_m_s2" in r.text                    # the handed-down one shows in «Роздано команде»


def test_iv_manager_reclaim_from_returns_to_manager():
    # TASK 2: bulk reclaim pulls a subordinate's собесы back to the MANAGER himself (responsible_id
    # → manager_id), with the same gender/direction/count filter as distribute — and the isolation
    # contract (a manager can only reclaim from HIS active subordinates).
    ids = _chain()
    i1 = db.allocate_interview("test_iv_m_rc1@x.com", ids["mgrA"], subject="rc1")
    i2 = db.allocate_interview("test_iv_m_rc2@x.com", ids["mgrA"], subject="rc2")
    db.manager_assign_interview(i1, ids["subS"])   # hand both down to subordinate S
    db.manager_assign_interview(i2, ids["subS"])
    _mark_announced()
    assert db.assigned_load([ids["subS"]]).get(ids["subS"]) == 2

    _login("test_iv_m_A")
    r = client.post("/manage/reclaim_from",
                    data={"member_id": ids["subS"], "count": 1}, follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    got = [db.interview_by_id(i)["responsible_id"] for i in (i1, i2)]
    assert got.count(ids["mgrA"]) == 1 and got.count(ids["subS"]) == 1   # exactly one pulled back

    # ISOLATION: a manager cannot reclaim from a non-subordinate (the plain employee E) — no-op
    r = client.post("/manage/reclaim_from",
                    data={"member_id": ids["empE"], "count": 1}, follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    got = [db.interview_by_id(i)["responsible_id"] for i in (i1, i2)]
    assert got.count(ids["mgrA"]) == 1 and got.count(ids["subS"]) == 1   # unchanged


def test_iv_manager_portal_new_controls_rendered():
    # the reclaim route + the Telegram-connect prompt (reusing /cabinet/tg/connect) are wired in.
    ids = _chain()
    db.allocate_interview("test_iv_m_nc@x.com", ids["mgrA"], subject="nc")
    _login("test_iv_m_A")
    r = client.get("/manage", follow_redirects=False)
    assert r.status_code == 200
    assert 'action="/manage/reclaim_from"' in r.text                 # bulk reclaim UI trigger
    # a manager with no linked Telegram sees the connect prompt, reusing the existing cabinet route
    assert 'action="/cabinet/tg/connect"' in r.text and "Подключить Telegram" in r.text


# ---- role editor: self-lockout guard (audit finding 3) ---------------------------
def test_iv_role_self_demote_admin_refused():
    # the acting admin must NOT be able to strip their OWN «admin» role (self-lockout). A
    # crafted POST that omits `admin` (the inline card renders it locked, but the server is the
    # real gate) is refused with a friendly notice and the account stays admin.
    _chain()
    _login("test_iv_m_admin")
    me = db.get_responsible_by_login("test_iv_m_admin")
    r = client.post(f"/users/{me['id']}/roles",
                    data={"role": ["manager"], "from_list": "1"}, follow_redirects=False)
    assert r.status_code == 200
    assert "самого себя" in r.text                                  # friendly refusal shown
    assert db.has_role(db.get_responsible(me["id"]), "admin")       # unchanged, still admin
    # the admin CAN still ADD roles to himself (admin retained) — the guard only blocks dropping it
    r = client.post(f"/users/{me['id']}/roles",
                    data={"role": ["admin", "manager"], "from_list": "1"}, follow_redirects=False)
    assert r.status_code == 200
    u = db.get_responsible(me["id"])
    assert db.has_role(u, "admin") and db.has_role(u, "manager")


def test_iv_role_demote_other_admin_allowed():
    # control: the self-guard is SELF-only — an admin may still demote a DIFFERENT account.
    ids = _chain()
    other = db.add_responsible("test_iv_m_adm2", auth.hash_password(_PW), "Adm2", role="admin")
    _login("test_iv_m_admin")
    r = client.post(f"/users/{other}/roles",
                    data={"role": ["manager"], "from_list": "1"}, follow_redirects=False)
    assert r.status_code == 200
    u = db.get_responsible(other)
    assert not db.has_role(u, "admin") and db.has_role(u, "manager")


# ---- allocate-send: blank manager is friendly, not a raw 422 (audit finding 1) ----
def test_iv_allocate_send_blank_manager_is_friendly():
    _chain()
    _login("test_iv_m_admin")
    # a blank manager pick (the select's empty first option) must yield the friendly notice
    r = client.post("/users/allocate/send",
                    data={"mailbox": "test_iv_x@takhet.com", "manager_id": ""},
                    follow_redirects=False)
    assert r.status_code == 200 and "Выберите управляющего" in r.text
    # a non-numeric manager_id is likewise friendly (int-validated in-body), never a 422
    r = client.post("/users/allocate/send",
                    data={"mailbox": "test_iv_x@takhet.com", "manager_id": "abc"},
                    follow_redirects=False)
    assert r.status_code == 200 and "Выберите управляющего" in r.text
    # a blank mailbox is friendly too
    r = client.post("/users/allocate/send",
                    data={"mailbox": "", "manager_id": "1"}, follow_redirects=False)
    assert r.status_code == 200 and "Укажите интервью" in r.text


# ---- distribute route wired to the portal button (audit finding 5) ---------------
def test_iv_manage_distribute_round_robin():
    ids = _chain()
    i1 = db.allocate_interview("test_iv_m_rr1@x.com", ids["mgrA"], subject="rr1")
    i2 = db.allocate_interview("test_iv_m_rr2@x.com", ids["mgrA"], subject="rr2")
    _login("test_iv_m_A")
    r = client.post("/manage/distribute", data={}, follow_redirects=False)
    _mark_announced()
    assert r.status_code == 200
    got = [db.interview_by_id(i)["responsible_id"] for i in (i1, i2)]
    # team = {manager A, active subordinate S}; 2 interviews round-robin one to each, none left
    assert set(got) == {ids["mgrA"], ids["subS"]}
    assert all(db.interview_by_id(i)["status"] == "assigned" for i in (i1, i2))


def test_iv_manage_distribute_button_rendered():
    # the previously-orphan POST /manage/distribute now has a UI trigger in the portal.
    ids = _chain()
    _login("test_iv_m_A")
    r = client.get("/manage", follow_redirects=False)
    assert r.status_code == 200
    assert 'action="/manage/distribute"' in r.text
    assert "Распределить всё поровну" in r.text


def test_iv_delete_protects_real_interviewers_and_self():
    from backend.interviews import routes_users
    assert routes_users._PROTECTED_LOGINS == {"1", "2", "3"}
    _chain()
    _login("test_iv_m_admin")
    me = db.get_responsible_by_login("test_iv_m_admin")

    # deleting yourself is refused (you're signed in as it)
    r = client.post(f"/users/{me['id']}/delete", follow_redirects=False)
    assert r.status_code == 200 and db.get_responsible(me["id"]) is not None

    # the real interviewers 1/2/3 are protected — the guard returns BEFORE any delete
    real = db.get_responsible_by_login("1")
    if real:  # present on the live DB
        r = client.post(f"/users/{real['id']}/delete", follow_redirects=False)
        assert r.status_code == 200
        assert db.get_responsible(real["id"]) is not None  # untouched
