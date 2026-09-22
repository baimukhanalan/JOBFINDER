"""Admin RECLAIM tools on the «Пользователи» tab — забрать интервью у пользователя обратно в
свободный пул (the inverse of the delegation split).

Covers `pool.reclaim` (count + gender/direction filter, `by=manager`/`responsible`) and
`db.reclaim_interview` (one specific interview by id), and PROVES the freed mailbox re-enters the
free pool (`pool.unallocated` sees it again).

Everything uses synthetic `test_iv_%` mailboxes/logins (never a real persona), like
test_interviews_manager.py. Two tests additionally seed a minimal interview-stage `mail_index` row
so the mailbox is a real member of `pool.unallocated` (its only inbound kind is 'interview' →
furthest stage 'interview'); every other membership check uses the cheap `db.handled_pool_mailboxes`
(the exact condition `pool.unallocated` excludes on), which needs no mail_index. This hits the LIVE
jobfinder_crm Postgres ALONGSIDE the pm2 daemons (indexer / ivremind / dash), so every WRITE + the
cleanup is wrapped in a deadlock-retry, and assigned rows are marked `announced=TRUE` immediately so
the Telegram notifier never pings the owner. Skipped if the DB is unreachable. Run SEQUENTIALLY with
the other test_interviews_*.

    sg mail -c 'PYTHONPATH=. python3 -m pytest backend/tests/test_interviews_users_reclaim.py -q'
"""
from __future__ import annotations

import time

import psycopg2
import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import auth, db, pool  # noqa: E402

_PW = "throwaway-test-pw-4471"
_TRANSIENT = (psycopg2.errors.DeadlockDetected, psycopg2.errors.SerializationFailure,
              psycopg2.OperationalError)


def _retry(fn, tries: int = 8):
    """Run a WRITE against the live CRM, retrying a transient deadlock/serialization with the live
    daemons (indexer/ivremind/dash). Re-raises the last error if it never clears."""
    last = None
    for _ in range(tries):
        try:
            return fn()
        except _TRANSIENT as e:
            last = e
            time.sleep(0.4)
    if last:
        raise last


def _cleanup():
    def _do():
        with mail_db._cur(dict_rows=False) as cur:
            cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
            cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")
            cur.execute("DELETE FROM mail_index WHERE mailbox LIKE 'test_iv_%'")
    _retry(_do)


def _mark_announced():
    def _do():
        with mail_db._cur(dict_rows=False) as cur:
            cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_%'")
    _retry(_do)


@pytest.fixture(autouse=True)
def _clean():
    db.ensure_schema()
    _cleanup()
    pool._invalidate()
    yield
    _mark_announced()
    _cleanup()
    pool._invalidate()


def _mgr(login: str, name: str) -> int:
    return _retry(lambda: db.add_responsible(login, auth.hash_password(_PW), name, role="manager"))


def _alloc(mailbox: str, manager_id: int, **kw) -> int:
    return _retry(lambda: db.allocate_interview(mailbox, manager_id, **kw))


def _seed_interview_mail(mailbox: str) -> None:
    """A minimal inbound interview-stage mail_index row so `mailbox` becomes a member of the free
    pool (`pool.unallocated`). A recent date_ts → the booking deadline is only ESTIMATED, never
    expired, so the row stays in the delegatable pool."""
    def _do():
        mail_db.upsert_message(
            mailbox=mailbox, candidate="Test Persona", candidate_id="",
            path=f"/tmp/test_iv/{mailbox}", path_hash=f"test_iv_hash_{mailbox}",
            from_name="Recruiter", from_email="recruiter@example-co.com",
            subject="Interview invitation", snippet="Let's schedule an interview.",
            kind="interview", thread_key="", has_att=False, outbound=False,
            date_ts=int(time.time()), seen=True)
    _retry(_do)


def _handled(mailbox: str) -> bool:
    """Cheap «out of the free pool» check — a mailbox with a non-cancelled iv row (the exact
    condition `pool.unallocated` excludes on). Needs no mail_index."""
    return mailbox in db.handled_pool_mailboxes()


def _in_unallocated(mailbox: str) -> bool:
    """Whether `pool.unallocated` (the enriched read the admin UI uses) currently lists `mailbox`.
    Retries a transient empty result (pool.unallocated swallows a DB hiccup → []; the seeded row is
    always present once committed)."""
    for _ in range(6):
        pool._invalidate()
        rows = pool.unallocated(limit=None)
        if any(r["mailbox"] == mailbox for r in rows):
            return True
        if rows:                     # a populated pool that simply doesn't contain it → decisive
            return False
        time.sleep(0.4)
    return False


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


# ---- pool.reclaim: count + the freed mailbox re-appears in pool.unallocated -------
def test_iv_reclaim_count_and_reappears_in_unallocated():
    mgr = _mgr("test_iv_rc_A", "Manager A")
    mbs = ["test_iv_rc_1@x.com", "test_iv_rc_2@x.com", "test_iv_rc_3@x.com"]
    for mb in mbs:
        _seed_interview_mail(mb)
        _alloc(mb, mgr, subject="iv")
    _mark_announced()
    # allocated → OUT of the free pool
    handled = db.handled_pool_mailboxes()
    assert all(mb in handled for mb in mbs)
    assert all(not _in_unallocated(mb) for mb in mbs)
    assert len(db.interviews_held_by(mgr, by="manager")) == 3

    # reclaim 2 (no filter) → exactly 2 freed, one still held
    freed = pool.reclaim(mgr, n=2, by="manager")
    assert freed == 2
    assert len(db.interviews_held_by(mgr, by="manager")) == 1
    back = [mb for mb in mbs if not _handled(mb)]
    assert len(back) == 2
    # pool.unallocated sees a freed mailbox again
    assert _in_unallocated(back[0])

    # reclaim the rest → 0 held, all three delegatable again
    freed = pool.reclaim(mgr, by="manager")
    assert freed == 1
    assert db.interviews_held_by(mgr, by="manager") == []
    assert not (db.handled_pool_mailboxes() & set(mbs))


# ---- pool.reclaim: gender/direction filter ---------------------------------------
def test_iv_reclaim_direction_filter_frees_only_matching():
    it_id, nonit_id = _it_nonit_jobids()
    if not it_id or not nonit_id:
        pytest.skip("no categorized jobs in job_catalog")
    mgr = _mgr("test_iv_rd_A", "Manager D")
    it1, it2, ni = "test_iv_rd_it1@x.com", "test_iv_rd_it2@x.com", "test_iv_rd_ni@x.com"
    # the reclaim direction filter reads each held row's OWN jobid → force it via allocate.
    for mb, jid in ((it1, it_id), (it2, it_id), (ni, nonit_id)):
        _alloc(mb, mgr, jobid=jid, subject="iv")
    _mark_announced()

    # a direction that matches NOTHING frees 0 and touches no rows
    assert pool.reclaim(mgr, direction="other", by="manager") == 0
    assert len(db.interviews_held_by(mgr, by="manager")) == 3

    # reclaim only the IT ones → 2 freed, the non-IT one stays held
    freed = pool.reclaim(mgr, direction="it", by="manager")
    assert freed == 2
    held = {r["mailbox"] for r in db.interviews_held_by(mgr, by="manager")}
    assert held == {ni}
    assert not _handled(it1) and not _handled(it2)   # the IT ones are back in the pool
    assert _handled(ni)                              # still held → not delegatable

    # a gender filter that matches nothing (these iv rows have no persona gender → 'unknown') frees 0
    assert pool.reclaim(mgr, gender="male", by="manager") == 0
    assert len(db.interviews_held_by(mgr, by="manager")) == 1


# ---- pool.reclaim: by=responsible reclaims only that interviewer's queue ----------
def test_iv_reclaim_by_responsible_scopes_to_own_queue():
    mgr = _mgr("test_iv_rr_A", "Manager R")
    sub = _retry(lambda: db.add_responsible("test_iv_rr_S", auth.hash_password(_PW), "Sub S",
                                            role="employee", manager_id=mgr))
    own, team = "test_iv_rr_own@x.com", "test_iv_rr_team@x.com"
    _alloc(own, mgr, subject="own")                            # manager is default attendee
    i_team = _alloc(team, mgr, subject="team")
    _retry(lambda: db.manager_assign_interview(i_team, sub))   # delegated to the subordinate
    _mark_announced()

    # reclaim by the SUBORDINATE (responsible) → only his one interview
    freed = pool.reclaim(sub, by="responsible")
    assert freed == 1
    assert not _handled(team) and _handled(own)
    # the manager still holds his own attendee interview
    assert {r["mailbox"] for r in db.interviews_held_by(mgr, by="manager")} == {own}


# ---- db.reclaim_interview: one specific interview by id, back in pool.unallocated -
def test_iv_reclaim_one_frees_exactly_that_mailbox():
    mgr = _mgr("test_iv_ro_A", "Manager O")
    keep, drop = "test_iv_ro_keep@x.com", "test_iv_ro_drop@x.com"
    for mb in (keep, drop):
        _seed_interview_mail(mb)
        _alloc(mb, mgr, subject="iv")
    _mark_announced()
    iid = next(r["id"] for r in db.interviews_held_by(mgr, by="manager") if r["mailbox"] == drop)

    freed_mb = _retry(lambda: db.reclaim_interview(iid))
    assert freed_mb == drop
    assert db.interview_by_id(iid) is None            # row gone
    assert not _handled(drop)                         # freed one is delegatable again
    assert _handled(keep)                             # the other stays held
    assert _in_unallocated(drop)                      # pool.unallocated sees it again
    assert _retry(lambda: db.reclaim_interview(iid)) is None   # idempotent: already gone


# ---- HTTP: the reclaim routes are wired + admin-only ------------------------------
def test_iv_reclaim_routes_over_http():
    from fastapi.testclient import TestClient
    from backend.dashboard_app import app
    client = TestClient(app)

    _retry(lambda: db.add_responsible("test_iv_rh_admin", auth.hash_password(_PW), "Admin",
                                      role="admin"))
    mgr = _mgr("test_iv_rh_A", "Manager H")
    mb1, mb2 = "test_iv_rh_1@x.com", "test_iv_rh_2@x.com"
    for mb in (mb1, mb2):
        _alloc(mb, mgr, subject="iv")
    _mark_announced()

    # login (retry a transient live-DB hiccup that would render the login page instead of a 303)
    cookie = None
    for _ in range(6):
        r = client.post("/login", data={"login": "test_iv_rh_admin", "password": _PW},
                        follow_redirects=False)
        if r.status_code == 303 and r.cookies.get(auth.COOKIE_NAME):
            cookie = r.cookies.get(auth.COOKIE_NAME)
            break
        time.sleep(0.5)
    assert cookie, "admin login did not return a session cookie"

    def _post(path, data):
        # a mid-sequence 303 is the auth middleware bouncing on a transient live-DB read of the
        # session's `active` flag — the session is valid, so re-set the cookie and retry.
        rr = None
        for _ in range(6):
            client.cookies.set(auth.COOKIE_NAME, cookie)
            rr = client.post(path, data=data, follow_redirects=False)
            if rr.status_code != 303:
                return rr
            time.sleep(0.5)
        return rr

    # a blank/garbage target is a friendly notice, never a raw 422
    r = _post("/users/reclaim/count", {"reclaim_user_id": ""})
    assert r.status_code == 200 and "Выберите пользователя" in r.text
    r = _post("/users/reclaim/one", {"mailbox": ""})
    assert r.status_code == 200 and "Укажите интервью" in r.text

    # reclaim ONE by email → its row is deleted (returned to the free pool)
    iid1 = next(r["id"] for r in db.interviews_held_by(mgr, by="manager") if r["mailbox"] == mb1)
    r = _post("/users/reclaim/one", {"mailbox": mb1})
    assert r.status_code == 200 and "возвращено в свободный пул" in r.text
    assert db.interview_by_id(iid1) is None
    assert {r["mailbox"] for r in db.interviews_held_by(mgr, by="manager")} == {mb2}

    # count reclaim: take all remaining of manager H → nothing left held
    r = _post("/users/reclaim/count", {"reclaim_user_id": mgr})
    _mark_announced()
    assert r.status_code == 200 and "Возвращено в свободный пул" in r.text
    assert db.interviews_held_by(mgr, by="manager") == []
