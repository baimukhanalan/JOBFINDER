"""Tests for the merged, Gmail-style «Кандидаты» screen.

Two layers are covered:

1. Pure RENDER (`backend.tools.candidates_inbox`) — no DB, no network. Synthetic group
   dicts are built inline via `_g(**over)`; every assertion is matched against the module's
   REAL markup (class names / active-state / hidden-toggle / escaping), so the tests lock in
   current behaviour rather than guessing.
2. DATA contract (`mail_db.candidate_groups` + its `mailcrm.candidate_groups` wrapper) —
   READ-ONLY, guarded, and skipped when no CRM DSN is reachable (mirrors
   test_interviews_notify.py). These make no writes.
"""
from __future__ import annotations

import pytest

from backend.tools import candidates_inbox as ci
from backend.tools import mail_db, mailcrm


# ---- shared key contracts ----------------------------------------------------------
# The keys mail_db.candidate_groups() puts on every row (the DB layer). The mailcrm
# wrapper ADDS name/id/is_demo on top of these.
_DB_KEYS = {
    "mailbox", "last_ts", "msg_count", "unread", "n_interview", "n_offer",
    "n_rejection", "n_action", "n_ack", "has_sent", "stage", "last_hash",
    "last_thread", "last_subject", "last_snippet", "last_from", "last_candidate",
    "last_kind", "last_outbound", "has_att", "iv_hash", "iv_thread",
}
_WRAPPER_KEYS = _DB_KEYS | {"name", "id", "is_demo"}


def _g(**over) -> dict:
    """A full, valid candidate-group dict (every contract key) with sane defaults,
    overridable per test."""
    g = {
        "mailbox": "a@takhet.com",
        "name": "Jane",
        "id": "a",
        "is_demo": True,
        "last_ts": 1000,
        "msg_count": 3,
        "unread": 0,
        "n_interview": 0,
        "n_offer": 0,
        "n_rejection": 0,
        "n_action": 0,
        "n_ack": 0,
        "has_sent": False,
        "stage": "other",
        "last_hash": "h",
        "last_thread": "t",
        "last_subject": "Hi",
        "last_snippet": "snip",
        "last_from": "Bob",
        "last_candidate": "Jane",
        "last_kind": "other",
        "last_outbound": False,
        "has_att": False,
        "iv_hash": "",
        "iv_thread": "",
    }
    g.update(over)
    return g


# ---- pure render: group cards ------------------------------------------------------
def test_render_groups_basic_card_with_sobes():
    out = ci.render_groups([_g(
        mailbox="a@takhet.com", name="Jane Roe", unread=2, msg_count=3,
        stage="interview", iv_hash="H1", iv_thread="T1")])
    assert 'data-mailbox="a@takhet.com"' in out
    assert "Jane Roe" in out
    assert "cg-card" in out
    # «Собес» control present BECAUSE iv_hash is set (renders openSobes(...) / .iv-sobes)
    assert "openSobes" in out
    assert "iv-sobes" in out


def test_sobes_control_absent_without_iv_hash():
    with_iv = ci.render_groups([_g(iv_hash="H1", iv_thread="T1")])
    without_iv = ci.render_groups([_g(iv_hash="")])
    assert "openSobes(" in with_iv
    assert "openSobes(" not in without_iv
    assert "iv-sobes" not in without_iv


def test_group_card_shows_assigned_when_present():
    out = ci.render_groups([_g(mailbox="x@takhet.com", iv_hash="H1", iv_thread="T1",
                               assigned={"thread_key": "T1", "responsible_name": "Иван Петров"})])
    assert "iv-assigned" in out
    assert "Назначено" in out
    assert "Иван Петров" in out
    # the assigned control replaces «Собес» (still opens the modal, but as edit mode)
    assert "iv-sobes" not in out


def test_group_card_shows_sobes_when_interview_but_not_assigned():
    out = ci.render_groups([_g(iv_hash="H1", iv_thread="T1")])  # no 'assigned' key
    assert "iv-sobes" in out
    assert "iv-assigned" not in out


def test_apps_chip_links_to_candidate_apps(monkeypatch):
    # Restored 2026-09-03: the résumé/applications «📄 N» chip → /candidates/<cid>.
    from backend.tools import candidate_apps
    monkeypatch.setattr(candidate_apps, "id_for_email", lambda e: "demo_x" if e else None)
    monkeypatch.setattr(candidate_apps, "app_count", lambda cid: 3)
    monkeypatch.setattr(candidate_apps, "resume_profile_ids", lambda: set())
    out = ci.render_groups([_g(mailbox="x@takhet.com")])
    assert 'class="cg-apps"' in out
    assert "📄 3" in out
    assert "/candidates/demo_x" in out
    assert "event.stopPropagation()" in out  # chip click must not toggle the card


def test_apps_chip_absent_without_resume_or_apps(monkeypatch):
    from backend.tools import candidate_apps
    # unknown mailbox → no cid → no chip
    monkeypatch.setattr(candidate_apps, "id_for_email", lambda e: None)
    monkeypatch.setattr(candidate_apps, "app_count", lambda cid: 0)
    monkeypatch.setattr(candidate_apps, "resume_profile_ids", lambda: set())
    assert 'class="cg-apps"' not in ci.render_groups([_g(mailbox="ghost@takhet.com")])
    # known cid but 0 apps AND no base résumé → still no chip (no dead link)
    monkeypatch.setattr(candidate_apps, "id_for_email", lambda e: "demo_y")
    assert 'class="cg-apps"' not in ci.render_groups([_g(mailbox="y@takhet.com")])


def test_unread_badge_only_when_positive():
    seven = ci.render_groups([_g(unread=7)])
    none = ci.render_groups([_g(unread=0)])
    # the distinctive number shows only in the unread=7 render
    assert "7" in seven
    assert "cg-cnt" in seven
    assert "cg-cnt" not in none


def test_render_groups_empty_input():
    assert ci.render_groups([]) == ""
    assert ci.render_groups(None) == ""


def test_render_groups_escapes_html():
    out = ci.render_groups([_g(name="<script>x</script>", last_subject="<b>hi</b>")])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    # subject is escaped too
    assert "<b>hi</b>" not in out


# ---- pure render: full page --------------------------------------------------------
def test_page_constant():
    assert ci.PAGE == 40


def test_render_page_shell_and_controls():
    page = ci.render_page([], tab="all", stage="", q="",
                          stage_counts={"all": 5, "interview": 2, "sent": 1})
    assert "<main" in page
    assert "Кандидаты" in page
    assert 'id="grouplist"' in page
    assert 'id="grpmore"' in page
    # «Приоритетные» tab was removed by owner request
    assert "Приоритетные" not in page
    # filters restored as a «Фильтры» button + modal; compose restored (mobile FAB + desktop btn)
    assert 'class="filter-btn"' in page
    assert 'id="cgFilterModal"' in page
    assert "fab-compose" in page
    assert "cg-compose-desk" in page
    assert "openCompose()" in page


def test_render_page_single_title_no_priority_link():
    page = ci.render_page([], tab="all",
                          stage_counts={"all": 5, "interview": 2, "sent": 1})
    # clean single title «Кандидаты» + mono total count; the old «Все письма» pseudo-tab is gone
    assert '<span class="cg-h">Кандидаты</span>' in page
    assert '<span class="cg-h-count">5</span>' in page
    assert "Все письма" not in page
    assert 'class="cg-tab' not in page
    # no priority tab link anywhere
    assert "tab=priority" not in page


def test_render_page_filter_modal_lists_stages_with_counts():
    page = ci.render_page([], tab="all",
                          stage_counts={"all": 5, "interview": 2, "sent": 1})
    # the mobile filter modal carries the stage options in the shared .fm-stage chrome
    assert "fm-stage" in page
    assert "Собеседование" in page and "Отказ" in page
    # a supplied count flows into the modal / funnel
    assert ">5<" in page


def test_render_page_funnel_counts():
    page = ci.render_page([], tab="all",
                          stage_counts={"all": 5, "interview": 2, "sent": 1})
    # the funnel renders each supplied count in a <b>…</b>
    assert "<b>5</b>" in page
    assert "<b>2</b>" in page
    assert "<b>1</b>" in page


def test_render_page_sentinel_hidden_when_no_more():
    import re
    page = ci.render_page([_g()], tab="all", has_more=False, offset=0)
    m = re.search(r'<div id="grpmore"[^>]*>', page)
    assert m, "sentinel must be present"
    assert "hidden" in m.group(0)


def test_render_page_sentinel_visible_and_advances_when_more():
    import re
    groups = [_g(mailbox=f"c{i}@takhet.com", id=f"c{i}") for i in range(40)]
    page = ci.render_page(groups, tab="all", has_more=True, offset=40)
    m = re.search(r'<div id="grpmore"([^>]*)>', page)
    assert m, "sentinel must be present"
    attrs = m.group(0)
    # NOT hidden when there is a next page
    assert "hidden" not in attrs
    # data-offset carries a number > 40 (offset 40 + 40 groups == 80)
    off = re.search(r'data-offset="(\d+)"', attrs)
    assert off and int(off.group(1)) > 40


def test_render_page_branding_neutral():
    groups = [_g(mailbox=f"c{i}@takhet.com", id=f"c{i}") for i in range(40)]
    page = ci.render_page(groups, tab="priority",
                          stage_counts={"all": 5, "interview": 2, "sent": 1},
                          has_more=True, offset=40)
    low = page.lower()
    for banned in ("claude", "anthropic", "openai", " gpt", "llm"):
        assert banned not in low, banned


# ---- pure render: thread + message fragments ---------------------------------------
def _m(**over) -> dict:
    m = {
        "id": "m1",
        "mailbox": "a@takhet.com",
        "from_name": "Alpha",
        "subject": "Subj",
        "snippet": "snip",
        "kind": "other",
        "thread": "t",
        "has_att": False,
        "outbound": False,
        "date_ts": 100,
        "seen": True,
    }
    m.update(over)
    return m


def test_render_thread_fragment_order_and_count():
    msg1 = _m(id="m1", subject="S1", date_ts=100, seen=True)
    msg2 = _m(id="m2", subject="S2", kind="interview", has_att=True, date_ts=200, seen=False)
    out = ci.render_thread_fragment("a@takhet.com", [msg2, msg1])
    # one row per message
    assert out.count("cg-msg-top") == 2
    # order preserved: msg2 before msg1
    assert out.index("m2") < out.index("m1")


def test_render_thread_fragment_empty():
    out = ci.render_thread_fragment("a@takhet.com", [])
    assert "Писем нет" in out


def test_render_message_fragment_body_and_defensive():
    good = ci.render_message_fragment({
        "plain": "HELLO_BODY_TEXT", "subject": "S", "date_ts": 100,
        "outbound": False, "mailbox": "a@takhet.com", "attachments": [], "html": "",
    })
    assert isinstance(good, str) and good
    assert "HELLO_BODY_TEXT" in good

    # a deliberately broken dict must NOT raise — degrades to some div
    broken = ci.render_message_fragment({})
    assert isinstance(broken, str)
    assert "div" in broken


# ---- live DB (read-only, skipped without a CRM DSN) --------------------------------
try:
    with mail_db._cur(dict_rows=False) as _c:
        _c.execute("SELECT 1")
    HAS_DB = True
except Exception:
    HAS_DB = False


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_db_candidate_groups_full_key_contract():
    rows = mail_db.candidate_groups(limit=3)
    for r in rows:
        assert _DB_KEYS <= set(r.keys()), sorted(_DB_KEYS - set(r.keys()))


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_db_candidate_groups_priority_filter():
    rows = mail_db.candidate_groups(stage="priority", limit=5)
    for r in rows:
        assert r.get("n_interview") or r.get("n_action")


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_db_candidate_groups_interview_filter_has_iv_hash():
    rows = mail_db.candidate_groups(stage="interview", limit=5)
    for r in rows:
        assert r.get("iv_hash")


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_db_candidate_groups_sorted_by_last_ts_desc():
    rows = mail_db.candidate_groups(limit=5)
    ts = [r["last_ts"] for r in rows]
    assert ts == sorted(ts, reverse=True)


@pytest.mark.skipif(not HAS_DB, reason="no CRM DB")
def test_mailcrm_candidate_groups_wrapper_adds_identity_keys():
    rows = mailcrm.candidate_groups(limit=2)
    for r in rows:
        assert "name" in r
        assert "is_demo" in r
        assert _WRAPPER_KEYS <= set(r.keys()), sorted(_WRAPPER_KEYS - set(r.keys()))


# ---- assessment status chip + mark/un-mark control -------------------------------------
def test_assessment_inner_done_shows_passed_and_revert():
    h = ci.assessment_inner("x@takhet.com", done=True)
    assert "Пройдено" in h
    assert "data-mark=\"0\"" in h        # the button un-marks
    assert "cgMarkAsmt" in h


def test_assessment_inner_pending_shows_remaining_and_mark():
    h = ci.assessment_inner("x@takhet.com", done=False)
    assert "Осталось" in h
    assert "Отметить" in h
    assert "data-mark=\"1\"" in h        # the button marks passed


def test_assessment_control_absent_without_assessment():
    # a candidate with neither a done nor a pending assessment gets no chip
    g = {"mailbox": "nobody@takhet.com", "n_assessment_done": 0, "n_asmt_pending": 0}
    assert ci._assessment_control(g) == ""


def test_assessment_control_pending_renders_chip():
    g = {"mailbox": "p@takhet.com", "n_assessment_done": 0, "n_asmt_pending": 1}
    h = ci._assessment_control(g)
    assert "cg-asmt-wrap" in h and "Осталось" in h
