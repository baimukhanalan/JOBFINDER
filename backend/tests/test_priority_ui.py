"""The shared interview priority/upcoming cards (`interviews/priority_ui.py`) — pure HTML
rendering over pre-enriched rows, no DB/network. Reused by /users, /manage and /cabinet."""
from backend.interviews import priority_ui as P


def _row(mb, direction, sal_lbl, days, *, estimated=False, expired=False, book=False,
         salv=0, age=None, invite_ts=1000, **extra):
    r = {"mailbox": mb, "candidate": mb.split("@")[0].title(), "direction": direction,
         "salary_label": sal_lbl, "deadline_days": days, "deadline_ts": (1 if days is not None else None),
         "deadline_estimated": estimated, "expired": expired, "has_booking": book,
         "salary_value": salv, "invite_ts": invite_ts}
    if age is not None:
        r["invite_age_days"] = age
    r.update(extra)
    return r


def test_priority_card_splits_it_vs_nonit_and_keeps_sort_context():
    rows = [_row("a@x", "it", "$120k", 2, salv=120000),
            _row("b@x", "nonit", "$40k", 5, salv=40000),
            _row("c@x", "other", "", None, estimated=True, age=4)]
    html = P.priority_card(rows, "urgency", "/manage?as=5", anchor="mg-pri")
    assert "ivp-card" in html and 'id="mg-pri"' not in html  # anchor is on the div id=
    assert "IT‑специальности" in html and "Простые (не‑IT)" in html
    # the sort toggle preserves the surface's existing query (?as=5) and marks the active mode
    assert "/manage?as=5&pool_sort=salary#mg-pri" in html
    assert "ivp-sortb active" in html and ">Срочность<" in html
    # deadline wording comes from interview_priority.deadline_text
    assert "осталось 2 дн" in html
    # an estimated (never-expired) row shows the invite age, not a fake countdown
    assert "инвайт 4 дн назад" in html


def test_priority_card_empty():
    html = P.priority_card([], "salary", "/cabinet", empty="Ничего нет.")
    assert "Ничего нет." in html and "ivp-card" in html


def test_upcoming_list_collapses_expired_and_shows_status():
    rows = [_row("a@x", "it", "$100k", 1, salv=100000),
            _row("b@x", "nonit", "$40k", -3, expired=True, salv=40000),
            _row("c@x", "other", "", 4, responsible_id=7)]

    def _st(r):
        return P.status_assigned("Иван") if r.get("responsible_id") else P.status_free()

    html = P.upcoming_list(rows, status_of=_st, anchor="u-live")
    # explicitly-expired rows are dropped into a collapsed «Истёкшие (N)» details
    assert "Истёкшие (1)" in html and "<details" in html
    # actionable (bookable) count in the section header excludes the expired one
    assert "ivp-n'>2<" in html or "ivp-n\">2<" in html or ">2</span>" in html
    # status chips
    assert "назначен: Иван" in html and "в пуле" in html


def test_status_chips_escape_names():
    assert "&lt;b&gt;" in P.status_assigned("<b>") and "ivp-st-set" in P.status_assigned("x")
    assert "ivp-st-mgr" in P.status_manager("m") and "ivp-st-free" in P.status_free()
