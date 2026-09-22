"""End-to-end workflow for the interview-portal delegation chain (2026-09-22 feature set).

Exercises the WHOLE data-layer flow the three portals sit on, in one sequence:
  admin allocates → a manager AUTO-OWNS everything given to him (owner directive
  «остаток автоматом переходит ему») → the manager hands собесы DOWN to a subordinate
  → pulls one back to himself → the admin RECLAIMS count/specific interviews back to the
  free pool. Plus the weekly-nudge recipient set + the global «все актуальные предстоящие»
  overview.

Route-level coverage of each portal lives in test_interviews_{manager,users_reclaim,
cabinet_inbox,notify}.py; this file is the cross-cutting contract that ties the foundation
(db.allocate_interview / manager_assign / manager_unassign / reclaim_interview /
interviews_held_by / responsibles_for_nudge, pool.reclaim / allocated_rows) together.

Hits the LIVE jobfinder_crm Postgres (skipped if unreachable). Every row uses a `test_iv_%`
prefix; a fixture cleans before + after and marks assigned rows announced=TRUE so the live
ivremind daemon never pings about throwaway interviews. Run SEQUENTIALLY with the other
test_interviews_*.
"""
from __future__ import annotations

import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import auth, db, pool  # noqa: E402

_PW = "throwaway-test-pw-e2e-71"


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
    yield
    _mark_announced()
    _cleanup()


def _row(iid: int) -> dict:
    return db.interview_by_id(iid)


def test_full_delegation_and_reclaim_chain():
    admin = db.add_responsible("test_iv_e2e_admin", auth.hash_password(_PW), "Admin", role="admin")
    mgrA = db.add_responsible("test_iv_e2e_A", auth.hash_password(_PW), "Manager A", role="manager")
    subS = db.add_responsible("test_iv_e2e_S", auth.hash_password(_PW), "Sub S",
                              role="employee", manager_id=mgrA)
    intI = db.add_responsible("test_iv_e2e_I", auth.hash_password(_PW), "Interviewer I",
                              role="employee")
    assert all((admin, mgrA, subS, intI))

    # --- admin allocates 3 собесы to manager A → manager AUTO-OWNS them (req 9) ---
    ids = [db.allocate_interview(f"test_iv_e2e_c{i}@takhet.com", mgrA,
                                 company="Acme", subject=f"Interview {i}")
           for i in range(3)]
    for iid in ids:
        r = _row(iid)
        assert r["responsible_id"] == mgrA, "manager must be the DEFAULT attendee (auto-own)"
        assert r["manager_id"] == mgrA
        assert r["status"] == "assigned"
        assert r["announced"] is True, "auto-allocation must not re-arm the per-row notifier"

    # his own undistributed queue = all 3
    assert len(db.interviews_held_by(mgrA, by="responsible")) == 3
    assert len(db.interviews_held_by(mgrA, by="manager")) == 3

    # --- manager hands ONE собес DOWN to his subordinate ---
    db.manager_assign_interview(ids[0], subS)
    r0 = _row(ids[0])
    assert r0["responsible_id"] == subS and r0["status"] == "assigned"
    assert r0["manager_id"] == mgrA, "the allocation to the manager is kept when delegating down"
    assert len(db.interviews_held_by(mgrA, by="responsible")) == 2   # one moved to the sub
    assert len(db.interviews_held_by(mgrA, by="manager")) == 3       # still all under him
    assert len(db.interviews_held_by(subS, by="responsible")) == 1

    # --- manager PULLS it back from the subordinate → returns to the manager himself ---
    db.manager_unassign_interview(ids[0])
    r0 = _row(ids[0])
    assert r0["responsible_id"] == mgrA, "unassign returns the собес to the MANAGER, not a null pool"
    assert r0["status"] == "assigned"
    assert len(db.interviews_held_by(mgrA, by="responsible")) == 3
    assert len(db.interviews_held_by(subS, by="responsible")) == 0

    # --- admin RECLAIMS a count back to the free pool (pool.reclaim, inverse of split) ---
    freed = pool.reclaim(mgrA, n=2, by="responsible")
    assert freed == 2
    assert len(db.interviews_held_by(mgrA, by="responsible")) == 1

    # --- admin RECLAIMS the last one SPECIFICALLY (by iid) ---
    remaining = db.interviews_held_by(mgrA, by="responsible")[0]
    freed_mbx = db.reclaim_interview(int(remaining["id"]))
    assert freed_mbx == remaining["mailbox"]
    assert db.interviews_held_by(mgrA, by="manager") == []

    # every reclaimed row is gone from the allocated overview
    live_ids = {r["id"] for r in pool.allocated_rows() if str(r.get("mailbox", "")).startswith("test_iv_e2e_")}
    assert live_ids == set(), "reclaimed собесы must leave the global «актуальные предстоящие» overview"


def test_weekly_nudge_recipient_set():
    mgrA = db.add_responsible("test_iv_e2e_nA", auth.hash_password(_PW), "Manager A", role="manager")
    intI = db.add_responsible("test_iv_e2e_nI", auth.hash_password(_PW), "Interviewer I",
                              role="employee")
    adm = db.add_responsible("test_iv_e2e_nAdm", auth.hash_password(_PW), "Admin", role="admin")

    # nobody linked yet → nobody is a nudge recipient; all three lack telegram
    assert not any(r["id"] in {mgrA, intI} for r in db.responsibles_for_nudge())
    missing_ids = {r["id"] for r in db.responsibles_missing_telegram()}
    assert {mgrA, intI} <= missing_ids
    assert adm not in missing_ids, "a pure admin is not a manager/employee nudge target"

    # link the manager + interviewer → they become recipients, drop out of the missing set
    db.set_telegram_chat(mgrA, 111111)
    db.set_telegram_chat(intI, 222222)
    nudge_ids = {r["id"] for r in db.responsibles_for_nudge()}
    assert {mgrA, intI} <= nudge_ids
    assert adm not in nudge_ids
    still_missing = {r["id"] for r in db.responsibles_missing_telegram()}
    assert mgrA not in still_missing and intI not in still_missing
