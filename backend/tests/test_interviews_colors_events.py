"""Per-user colour + the SHARED «События найма» claim board.

Feature 1 — every responsible gets a STABLE, distinct colour (palette pick by id, or an
explicit `iv_responsibles.color` override) so managers/interviewers don't blur into one
colour on the «актуальные предстоящие» overviews.

Feature 2 — a hiring-event board open to every role (admin/управляющий/интервьюер) where
anyone can mark «я подключусь к этому кандидату в X:XX / уже подключился», visible to all.

Hits the LIVE jobfinder_crm Postgres (via mail_db's pool) — skipped if unreachable. Every
seeded row uses a `test_iv_%` login/mailbox prefix, cleaned up before AND after; any
assigned interview rows are marked `announced=TRUE` so the live Telegram notifier never
pings the owner. Run SEQUENTIALLY with the other `test_interviews_*`.
"""
from __future__ import annotations

import re
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
from backend.tools import hiring_events  # noqa: E402

client = TestClient(app)

_PW = "throwaway-test-pw-2291"
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _cleanup():
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("DELETE FROM iv_event_claims WHERE mailbox LIKE 'test_iv_%'")
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
    admin = db.add_responsible("test_iv_ce_admin", auth.hash_password(_PW), "Admin CE", role="admin")
    mgrA = db.add_responsible("test_iv_ce_A", auth.hash_password(_PW), "Manager CE", role="manager")
    empE = db.add_responsible("test_iv_ce_E", auth.hash_password(_PW), "Employee CE", role="employee")
    empF = db.add_responsible("test_iv_ce_F", auth.hash_password(_PW), "Employee FF", role="employee")
    return {"admin": admin, "mgrA": mgrA, "empE": empE, "empF": empF}


# ---- Feature 1: per-user colour --------------------------------------------------
def test_iv_color_for_stable_distinct_and_override():
    ids = _chain()
    # every user resolves to a valid hex, stable across calls
    seen = {}
    for uid in ids.values():
        resp = db.get_responsible(uid)
        c1 = db.color_for(resp)
        c2 = db.color_for(db.get_responsible(uid))
        assert _HEX.match(c1), c1
        assert c1 == c2                      # stable
        seen[uid] = c1
    # four consecutively-created ids (≤ palette length) get DISTINCT colours
    assert len(set(seen.values())) == len(seen)
    # add_responsible stored the palette colour on the row (not just a runtime fallback)
    assert db.get_responsible(ids["mgrA"]).get("color") == seen[ids["mgrA"]]
    # a bare dict with only an id still colours deterministically; an explicit colour wins
    assert db.color_for({"id": ids["empE"]}) == db.color_for(db.get_responsible(ids["empE"]))
    assert db.color_for({"id": 1, "color": "#abcdef"}) == "#abcdef"
    assert db.color_for({"id": 1, "color": "not-a-colour"}) == db._palette_color(1)
    # explicit set_color override round-trips through color_for
    db.set_color(ids["empF"], "#0c47c2")
    assert db.color_for(db.get_responsible(ids["empF"])) == "#0c47c2"


def test_iv_priority_status_chip_tinted_by_colour():
    from backend.interviews import priority_ui
    # a colour tints the chip inline; no colour keeps the neutral class (free chip untinted)
    tinted = priority_ui.status_manager("Иван", color="#2563eb")
    assert "#2563eb" in tinted and "ivp-st-mgr" in tinted
    assert "style=" not in priority_ui.status_manager("Иван")
    assert "style=" not in priority_ui.status_free()
    assert "&lt;b&gt;" in priority_ui.status_assigned("<b>", color="#dc2626")  # escapes the name


# ---- Feature 2: shared hiring-event claims ---------------------------------------
def test_iv_event_claim_roundtrip_two_owners():
    ids = _chain()
    mbx = "test_iv_ce_cand1@takhet.com"
    when = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=3)
    db.set_event_claim(mbx, ids["mgrA"], join_ts=when, joined=False)
    db.set_event_claim(mbx, ids["empE"], joined=True)

    got = db.event_claims_for([mbx])
    assert mbx in got
    by_id = {c["responsible_id"]: c for c in got[mbx]}
    assert set(by_id) == {ids["mgrA"], ids["empE"]}          # both owners on ONE candidate
    # each carries the owner's own colour (matches db.color_for)
    assert by_id[ids["mgrA"]]["color"] == db.color_for(db.get_responsible(ids["mgrA"]))
    assert by_id[ids["empE"]]["color"] == db.color_for(db.get_responsible(ids["empE"]))
    assert by_id[ids["empE"]]["joined"] is True
    assert by_id[ids["mgrA"]]["join_ts"] is not None

    # upsert: toggling joined WITHOUT a time keeps the existing time (COALESCE)
    db.set_event_claim(mbx, ids["mgrA"], joined=True)
    m = {c["responsible_id"]: c for c in db.event_claims_for([mbx])[mbx]}[ids["mgrA"]]
    assert m["joined"] is True and m["join_ts"] is not None

    # clear one → only the other remains
    db.clear_event_claim(mbx, ids["mgrA"])
    left = db.event_claims_for([mbx])[mbx]
    assert [c["responsible_id"] for c in left] == [ids["empE"]]


def test_iv_claim_post_records_acting_user():
    ids = _chain()
    _login("test_iv_ce_E")                       # a plain interviewer
    mbx = "test_iv_ce_post@takhet.com"
    r = client.post("/hiring-events/claim",
                    data={"mailbox": mbx, "join_local": "14:30", "joined": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    got = db.event_claims_for([mbx])
    assert mbx in got and len(got[mbx]) == 1
    c = got[mbx][0]
    assert c["responsible_id"] == ids["empE"]
    assert c["joined"] is True
    assert c["join_ts"] is not None
    # the acting user cannot claim on someone else's behalf (no ?as spoof) — unclaim clears it
    r2 = client.post("/hiring-events/unclaim", data={"mailbox": mbx}, follow_redirects=False)
    assert r2.status_code == 303
    assert db.event_claims_for([mbx]) == {}


def test_iv_shared_board_open_for_all_roles(monkeypatch):
    ids = _chain()
    monkeypatch.setattr(hiring_events, "grouped_events", lambda *a, **k: [])
    for login, name in (("test_iv_ce_admin", "Admin CE"),
                        ("test_iv_ce_A", "Manager CE"),
                        ("test_iv_ce_E", "Employee CE")):
        _login(login)
        r = client.get("/hiring-events", follow_redirects=False)
        assert r.status_code == 200, f"{name} could not open the shared board"
        assert "События найма" in r.text


def test_iv_render_page_shows_claim_chip_and_control():
    """Pure render: a claimer chip is shown to everyone (coloured); the acting user gets the
    inline «Я подключусь / Отменить» control."""
    ids = _chain()
    me = db.get_responsible(ids["empE"])
    mbx = "test_iv_ce_r@takhet.com"
    import time as _t
    groups = [{
        "key": "m1", "meeting_id": "999", "join_url": "https://us06web.zoom.us/j/999",
        "role": "Remote CSR", "date_text": "Monday–Friday", "time_text": "9:30 AM – 5:00 PM ET",
        "latest_ts": 0,
        "invites": [{"mailbox": mbx, "candidate": "Ray", "date_ts": 0, "path_hash": "rh1",
                     "tracking_url": "https://tracking.icims.com/f/a/A~~/x/t",
                     "join_url": "https://us06web.zoom.us/j/999",
                     "meeting_id": "999", "resolved": True,
                     "date_text": "Monday–Friday", "time_text": "9:30 AM – 5:00 PM ET",
                     "deadline_ts": int(_t.time()) + 2 * 86400, "deadline_days": 2,
                     "deadline_estimated": False}],
    }]
    claims = {mbx: [{"responsible_id": ids["mgrA"], "name": "Manager CE",
                     "color": "#7c3aed", "join_ts": None, "joined": True}]}
    html = hiring_events.render_page(groups, claims=claims, me=me, color_for=db.color_for)
    assert "Manager CE" in html and "#7c3aed" in html         # coloured claimer chip
    assert "подключился ✓" in html
    assert '/hiring-events/claim' in html                       # the acting user's control
    assert "Я подключусь" in html
    # the per-candidate window + deadline chip both render on the card
    assert "Окно: Пн–Пт 9:30 – 17:00 ET" in html
    assert "осталось 2 дн" in html


def test_iv_hiring_window_label():
    assert hiring_events._window_label(
        "Monday–Friday", "9:30 AM – 5:00 PM ET") == "Пн–Пт 9:30 – 17:00 ET"
    assert hiring_events._window_label("", "") == ""
    assert hiring_events._window_label("Weekdays", "") == "Weekdays"   # graceful fallback


def test_iv_hiring_deadline_chip():
    import time as _t
    now = int(_t.time())
    # an EXPLICIT past deadline reads «поздно», dim — and ONLY when not estimated
    assert hiring_events._deadline_chip_parts(
        {"deadline_ts": now - 3 * 86400, "deadline_days": -3,
         "deadline_estimated": False}) == ("поздно подключаться", "over")
    lbl, _lvl = hiring_events._deadline_chip_parts(
        {"deadline_ts": now - 3 * 86400, "deadline_days": -3, "deadline_estimated": True})
    assert lbl != "поздно подключаться"                     # estimated past is never «поздно»
    # near deadlines are a countdown; far ones a «до <дата>»
    assert hiring_events._deadline_chip_parts(
        {"deadline_ts": now + 86400, "deadline_days": 1,
         "deadline_estimated": False}) == ("осталось 1 дн", "urgent")
    assert hiring_events._deadline_chip_parts(
        {"deadline_ts": now, "deadline_days": 0, "deadline_estimated": False})[0] == "сегодня"
    far, _ = hiring_events._deadline_chip_parts(
        {"deadline_ts": now + 20 * 86400, "deadline_days": 20, "deadline_estimated": True})
    assert far.startswith("до ")
    assert hiring_events._deadline_chip_parts({}) == ("", "")
