"""STRESS + E2E for the «Interview Cockpit» chain (auto_reserve → calendar → prep) and the portal
UI/UX guarantees. Hits the LIVE jobfinder_crm Postgres (skipped if unreachable). Every seeded row
uses a `test_iv_%` prefix, cleaned before+after, and forced `announced=TRUE` on teardown so the live
ivremind daemon never DMs the owner. Run SEQUENTIALLY with the other `test_interviews_*`.

Covers what the unit suites didn't: auto_reserve at volume (idempotency, the (responsible_id,start_ts)
double-book guard, every slot inside 18:30–21:30 and non-colliding, speed), build_pack hammered over
many mailboxes incl. résumé-pruned ones (must never raise), and the no-admin-leak / no-stack-disclosure
invariants on the user portals.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.tools import mail_db

try:
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
except Exception:
    pytest.skip("no CRM DB", allow_module_level=True)

from backend.interviews import auth, db, auto_reserve, interview_prep  # noqa: E402

_PW = "throwaway-test-pw-7742"
_PFX = "test_iv_cs_"


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_interviews WHERE mailbox LIKE 'test_iv_%'")
        cur.execute("DELETE FROM iv_responsibles WHERE login LIKE 'test_iv_%'")


def _announce():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE WHERE mailbox LIKE 'test_iv_%'")


@pytest.fixture(autouse=True)
def _clean():
    db.ensure_schema()
    _cleanup()
    yield
    _announce()
    _cleanup()


def _mk_interviewer(login: str, name: str) -> int:
    rid = db.add_responsible(login, auth.hash_password(_PW), name, role="employee")
    with mail_db._cur(dict_rows=False) as cur:          # mirror Alan's real tz (non-UTC) to exercise localization
        cur.execute("UPDATE iv_responsibles SET tz='Europe/Berlin' WHERE id=%s", (rid,))
    return rid


def _seed_unscheduled(rid: int, n: int) -> list[int]:
    """n assigned, announced, NO start_ts interviews for `rid` (the reserve step's input)."""
    ids = []
    with mail_db._cur(dict_rows=False) as cur:
        for i in range(n):
            mb = f"{_PFX}{i}@takhet.com"
            cur.execute(
                """INSERT INTO iv_interviews (mailbox, thread_key, company, jobid, responsible_id,
                       status, announced) VALUES (%s,%s,%s,%s,%s,'assigned',TRUE) RETURNING id""",
                (mb, f"t{i}", f"corp{i % 7}", str(1000 + i), rid))
            ids.append(cur.fetchone()[0])
    return ids


def _tmp_state(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_reserve, "_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(auto_reserve, "_MANUAL", str(tmp_path / "manual.json"))


# --------------------------------------------------------------------------- slot purity
def test_slot_order_in_window_and_proximity():
    order = auto_reserve._slot_order()
    assert order[0] == auto_reserve.TARGET_MIN                       # 20:00 first
    assert all(auto_reserve.WINDOW_START_MIN <= m <= auto_reserve.WINDOW_END_MIN for m in order)
    # strictly ordered by closeness to 20:00
    import_dist = [abs(m - auto_reserve.TARGET_MIN) for m in order]
    assert import_dist == sorted(import_dist)


def test_candidate_slots_day_major_in_window():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    slots = auto_reserve.candidate_slots(tz, now=now, days=5)
    assert slots, "no slots produced"
    for s in slots:
        loc = s.astimezone(tz)
        mod = loc.hour * 60 + loc.minute
        assert auto_reserve.WINDOW_START_MIN <= mod <= auto_reserve.WINDOW_END_MIN
    assert len(slots) == len(set(slots))                             # no dup instants


# --------------------------------------------------------------------------- stress: reserve at volume
def test_reserve_120_idempotent_no_collision_in_window(monkeypatch, tmp_path):
    rid = _mk_interviewer(f"{_PFX}resv", "CS Reserver")
    _seed_unscheduled(rid, 120)
    monkeypatch.setattr(auto_reserve, "RESPONSIBLE_ID", rid)
    _tmp_state(monkeypatch, tmp_path)
    tz = auto_reserve._resp_tz(rid)                                  # assert in the responsible's OWN tz

    t0 = time.time()
    res = auto_reserve.run(dry_run=False)
    elapsed = time.time() - t0
    assert elapsed < 20, f"reserve too slow: {elapsed:.1f}s"
    # assign must NOT touch the live pool — the queue is far above the low-water mark
    assert res["skipped_assign"] and "queue not low" in res["skipped_assign"]
    assert res["assigned"] is None

    def _scheduled():
        with mail_db._cur() as cur:
            cur.execute("SELECT id, start_ts FROM iv_interviews WHERE responsible_id=%s "
                        "AND mailbox LIKE %s AND start_ts IS NOT NULL", (rid, f"{_PFX}%"))
            return {r["id"]: r["start_ts"] for r in cur.fetchall()}

    sched = _scheduled()
    assert sched, "nothing got scheduled"
    starts = list(sched.values())
    assert len(starts) == len(set(starts)), "DOUBLE-BOOK: two собесы share a start_ts"
    for st in starts:
        loc = st.astimezone(tz)
        mod = loc.hour * 60 + loc.minute
        assert auto_reserve.WINDOW_START_MIN <= mod <= auto_reserve.WINDOW_END_MIN, f"slot {loc} out of window"

    # idempotent: a 2nd run never re-times an already-scheduled row and never collides
    auto_reserve.run(dry_run=False)
    sched2 = _scheduled()
    for iid, st in sched.items():
        assert sched2.get(iid) == st, "a scheduled собес was re-timed on re-run"
    assert len(set(sched2.values())) == len(sched2), "collision after re-run"


def test_reserve_within_capacity_schedules_all(monkeypatch, tmp_path):
    rid = _mk_interviewer(f"{_PFX}cap", "CS Cap")
    ids = _seed_unscheduled(rid, 20)                                  # well under the 14-day grid capacity
    monkeypatch.setattr(auto_reserve, "RESPONSIBLE_ID", rid)
    _tmp_state(monkeypatch, tmp_path)
    auto_reserve.run(dry_run=False)
    with mail_db._cur() as cur:
        cur.execute("SELECT count(*) n FROM iv_interviews WHERE mailbox LIKE %s AND start_ts IS NOT NULL",
                    (f"{_PFX}%",))
        assert cur.fetchone()["n"] == len(ids), "not every собес within capacity was scheduled"


def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    rid = _mk_interviewer(f"{_PFX}dry", "CS Dry")
    _seed_unscheduled(rid, 10)
    monkeypatch.setattr(auto_reserve, "RESPONSIBLE_ID", rid)
    _tmp_state(monkeypatch, tmp_path)
    auto_reserve.run(dry_run=True)
    with mail_db._cur() as cur:
        cur.execute("SELECT count(*) n FROM iv_interviews WHERE mailbox LIKE %s AND start_ts IS NOT NULL",
                    (f"{_PFX}%",))
        assert cur.fetchone()["n"] == 0, "dry-run reserved a slot"


# --------------------------------------------------------------------------- build_pack hammer
def test_build_pack_never_raises_over_many_mailboxes(monkeypatch, tmp_path):
    rid = _mk_interviewer(f"{_PFX}pack", "CS Pack")
    _seed_unscheduled(rid, 40)
    mbs = [f"{_PFX}{i}@takhet.com" for i in range(40)]
    # + résumé-pruned real-shaped + a garbage mailbox
    mbs += ["marco.fernandez242@takhet.com", "does.not.exist9999@takhet.com", "", "no-at-sign"]
    for mb in mbs:
        p = interview_prep.build_pack(mb)                            # must never raise
        assert isinstance(p.brief, str) and p.brief
        assert p.mailbox == mb


# --------------------------------------------------------------------------- portal UI/UX invariants
def _client():
    from fastapi.testclient import TestClient
    from backend.dashboard_app import app
    return TestClient(app)


def _login(client, login):
    for _ in range(12):
        client.cookies.clear()
        r = client.post("/login", data={"login": login, "password": _PW}, follow_redirects=False)
        if r.status_code == 303 and r.cookies.get(auth.COOKIE_NAME):
            client.cookies.set(auth.COOKIE_NAME, r.cookies.get(auth.COOKIE_NAME))
            return True
        time.sleep(0.5)
    return False


def _stable_get(client, url, tries=20):
    r = None
    for _ in range(tries):
        r = client.get(url, follow_redirects=False)
        transient = (r.status_code in (302, 303, 307)
                     or (r.status_code == 200 and 'action="/login"' in r.text))
        if not transient:
            return r
        time.sleep(0.8)
    return r


_STACK_TOKENS = ("claude", "anthropic", "openai", "chatgpt", "gpt-", "llm", " ии ", "ии.", "ии,")


def test_user_portals_no_admin_leak_no_stack_disclosure():
    rid = _mk_interviewer(f"{_PFX}emp", "CS Employee")
    client = _client()
    if not _login(client, f"{_PFX}emp"):
        pytest.skip("login did not settle")
    for url in ("/cabinet", "/cabinet/guide", "/hiring-events"):
        r = _stable_get(client, url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        low = r.text.lower()
        for admin in ('href="/stats"', 'href="/users"', 'href="/catalog"', 'href="/health"',
                      'href="/mail/candidates"'):
            assert admin not in r.text, f"admin nav leaked on {url}: {admin}"
        for tok in _STACK_TOKENS:
            assert tok not in low, f"stack-disclosure token {tok!r} on {url}"


def test_reserved_собес_shows_on_cabinet_calendar(monkeypatch, tmp_path):
    rid = _mk_interviewer(f"{_PFX}cal", "CS Cal")
    _seed_unscheduled(rid, 3)
    monkeypatch.setattr(auto_reserve, "RESPONSIBLE_ID", rid)
    _tmp_state(monkeypatch, tmp_path)
    auto_reserve.run(dry_run=False)                                  # reserve ~20:00 slots
    client = _client()
    if not _login(client, f"{_PFX}cal"):
        pytest.skip("login did not settle")
    r = _stable_get(client, "/cabinet")
    assert r.status_code == 200
    assert "wk-grid" in r.text                                       # the week calendar rendered
    # at least one reserved row carries an evening (20:xx / 19:xx / 21:xx) time on the page
    assert any(f"{h}:" in r.text for h in (18, 19, 20, 21))
