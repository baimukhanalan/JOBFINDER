"""FastAPI routes for the manager («Управляющий») portal at /manage.

Mounted on the dashboard app. The dashboard AdminAuthMiddleware already confines a
`role='manager'` session to /manage/* + /cabinet/* (and bounces employees), so these
handlers only ever run for a manager acting on his OWN portal, or for an ADMIN doing a
read-through of a specific manager via `?as=<manager_id>` (admins control everything).

Isolation (defence in depth, not only the gate): every mutating handler re-checks that the
interview being touched is allocated to the ACTING manager (manager_id match) and that the
chosen interviewer is the manager himself or one of HIS subordinates. So a manager can never
assign an interview outside his pool, nor to someone else's team.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from html import escape

import psycopg2
from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.interviews import auth, db, manage_ui, pool, slots

router = APIRouter()

# `as` is a Python keyword, so the admin read-through's `?as=`/hidden field can't be a bare
# param name — bind it via an alias. Without the alias FastAPI looks for a field literally
# named `as_` and the admin read-through would silently lose its manager context.


def _acting(me: dict, as_id: str | int | None):
    """Resolve the manager whose portal is being acted on (multi-role aware).

    Returns (manager_row, is_admin_view) or (None, is_admin_view). An ADMIN who passes a
    valid `?as=<manager_id>` does a read-through of that manager. Otherwise a user who holds
    the 'manager' role acts on his OWN portal (as_id ignored — a non-admin can't spoof it).
    An admin with no valid manager target gets (None, True) → the caller redirects to /users."""
    is_admin = db.has_role(me, "admin")
    if as_id and is_admin:
        try:
            m = db.get_responsible(int(as_id))
        except (TypeError, ValueError):
            m = None
        if m and db.has_role(m, "manager"):
            return m, True
        return None, True
    if db.has_role(me, "manager"):
        return me, False
    if is_admin:
        return None, True
    return None, False


def _render(manager: dict, is_admin_view: bool, notice=None,
            q: str = "", gender: str = "", direction: str = "",
            pool_sort: str = "salary") -> HTMLResponse:
    mid = manager["id"]
    subs = db.subordinates(mid, active_only=False)
    sub_ids = {s["id"] for s in subs}
    # NEW allocation model (owner directive 2026-09-22): everything the admin allocates to a
    # manager is IMMEDIATELY his (allocate_interview → responsible_id=manager_id). So there is no
    # responsible-NULL «pool» for a manager any more. His собесы split into:
    #   • own  — still HIS to attend, not yet handed to the team (responsible_id == mid)
    #   • team — handed DOWN to a subordinate (responsible_id ∈ his team)
    # enrich with gender/direction + the priority signals (deadline, salary, expired) so the
    # filter, priority card and «актуальные предстоящие» read the same urgency as «Собес».
    interviews = pool.enrich_priority(db.manager_interviews(mid))
    own = [iv for iv in interviews if iv.get("responsible_id") == mid]
    team_ivs = [iv for iv in interviews if iv.get("responsible_id") in sub_ids]
    g = gender if gender in pool.GENDERS else None
    d = direction if direction in pool.DIRECTIONS else None
    own_shown = [iv for iv in own if pool._match(iv, (q or None), g, d)]
    loads = db.assigned_load([s["id"] for s in subs])
    # names for attendee rows: the manager + every subordinate (assignment is confined to them)
    names = {mid: manager.get("name") or manager.get("login") or "—"}
    for s in subs:
        names[s["id"]] = s.get("name") or s.get("login") or "—"
    counts = {"own": len(own), "own_shown": len(own_shown),
              "filtered": bool((q or "").strip() or g or d),
              "team": len(team_ivs), "total": len(interviews),
              "team_size": len([s for s in subs if s.get("active")])}
    # sort_base = this /manage URL WITH its current query minus pool_sort, so the priority-card
    # sort toggle keeps the admin read-through (?as) + the filter (?q/&gender/&direction).
    from urllib.parse import urlencode
    qs = {k: v for k, v in (("as", mid if is_admin_view else ""), ("q", q),
                            ("gender", gender), ("direction", direction)) if v}
    sort_base = "/manage" + ("?" + urlencode(qs) if qs else "")
    # TG-connect prompt: only in a manager's OWN view (an admin read-through can't — /cabinet/tg/
    # connect links the LOGGED-IN user's chat, and the manager, not the admin, must press Start).
    tg_missing = (not is_admin_view) and not manager.get("telegram_chat_id")
    return HTMLResponse(manage_ui.portal_page(
        manager, subs, own_shown, team_ivs, loads, names, counts,
        q=q, gender=gender, direction=direction, notice=notice,
        is_admin_view=is_admin_view, pool_all=own, scope_ivs=interviews,
        pool_sort=pool_sort, sort_base=sort_base, tg_missing=tg_missing))


def _allowed_interviewer_ids(manager: dict) -> set:
    """The manager himself + his ACTIVE subordinates — the only people he may assign to."""
    ids = {manager["id"]}
    ids |= {s["id"] for s in db.subordinates(manager["id"], active_only=True)}
    return ids


@router.get("/manage", response_class=HTMLResponse)
def manage_home(as_: str = Query("", alias="as"), q: str = Query(""),
                gender: str = Query(""), direction: str = Query(""),
                pool_sort: str = Query("salary"),
                me: dict = Depends(auth.current_responsible)):
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        # an admin with no (valid) target: send them to the roster where the managers +
        # their read-through links live.
        return RedirectResponse("/users", status_code=303)
    return _render(manager, is_admin_view, q=q.strip(), gender=gender, direction=direction,
                   pool_sort=pool_sort)


@router.post("/manage/subordinate/add", response_class=HTMLResponse)
def subordinate_add(name: str = Form(...), login: str = Form(...),
                    password: str = Form(""), as_: str = Form("", alias="as"),
                    me: dict = Depends(auth.current_responsible)):
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)
    name, login = name.strip(), login.strip()
    if not name or not login:
        return _render(manager, is_admin_view, ("err", "Имя и логин обязательны."))
    pw = password.strip() or secrets.token_urlsafe(9)
    try:
        db.add_responsible(login, auth.hash_password(pw), name, role="employee",
                           tz="Asia/Almaty", manager_id=manager["id"])
    except Exception as e:
        return _render(manager, is_admin_view,
                       ("err", f"Не удалось создать (логин, возможно, занят): {escape(str(e))}"))
    return _render(manager, is_admin_view, ("pw",
        f"Добавлен сотрудник <b>{escape(name)}</b> (логин <b>{escape(login)}</b>). "
        f"Пароль: <code>{escape(pw)}</code> — сохраните, он больше не покажется."))


@router.post("/manage/assign", response_class=HTMLResponse)
def manage_assign(iid: int = Form(...), responsible_id: int = Form(...),
                  start_local: str = Form(""), as_: str = Form("", alias="as"),
                  me: dict = Depends(auth.current_responsible)):
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)

    iv = db.interview_by_id(iid)
    # the interview MUST be allocated to THIS manager, and the interviewer MUST be him or
    # one of his subordinates — the isolation contract, enforced here not just at the gate.
    if not iv or iv.get("manager_id") != manager["id"] or iv.get("status") == "cancelled":
        return _render(manager, is_admin_view, ("err", "Собес не из вашего пула."))
    if responsible_id not in _allowed_interviewer_ids(manager):
        return _render(manager, is_admin_view,
                       ("err", "Можно назначать только себе или своим сотрудникам."))

    start_ts = end_ts = None
    if start_local.strip():
        try:
            naive = datetime.fromisoformat(start_local.strip())
        except ValueError:
            return _render(manager, is_admin_view, ("err", "Неверный формат времени."))
        # interpret the picked wall-clock in the INTERVIEWER's timezone (availability is
        # anchored there), store absolute UTC.
        target = db.get_responsible(responsible_id) or {}
        rtz = target.get("tz") or slots.DEFAULT_TZ
        start_ts = naive.replace(second=0, microsecond=0, tzinfo=slots.zone(rtz)).astimezone(slots.UTC)
        end_ts = start_ts + timedelta(minutes=slots.DURATION_MIN)
        # AVAILABILITY + OVERLAP gate (mirror service.assign): the manager UPDATE had ONLY the
        # exact-start partial-unique guard, so an OVERLAPPING slot or one outside the interviewer's
        # availability could double-book. Exclude this interview's own current booking (reassign).
        avail = db.get_availability(responsible_id)
        booked = db.booked_intervals(responsible_id, start_ts - timedelta(days=1),
                                     start_ts + timedelta(days=1), exclude_id=iid)
        if not slots.is_free_at(avail, rtz, booked, start_ts):
            return _render(manager, is_admin_view,
                           ("err", "Сотрудник недоступен в это время (нет окна или пересечение "
                                   "с другим собесом) — выберите другое."))

    try:
        db.manager_assign_interview(iid, responsible_id, start_ts, end_ts)
    except psycopg2.IntegrityError:
        # the partial unique index tripped: this interviewer already has a собес at that
        # exact time. Neutral message, no stack detail.
        return _render(manager, is_admin_view,
                       ("err", "У сотрудника уже есть собес на это время — выберите другое."))
    return _render(manager, is_admin_view, ("ok", "Собеседование назначено."))


@router.post("/manage/unassign", response_class=HTMLResponse)
def manage_unassign(iid: int = Form(...), as_: str = Form("", alias="as"),
                    me: dict = Depends(auth.current_responsible)):
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)
    iv = db.interview_by_id(iid)
    # reject a cancelled row too (mirror manage_assign) — else a crafted POST could resurrect a
    # cancelled interview back into the pool.
    if not iv or iv.get("manager_id") != manager["id"] or iv.get("status") == "cancelled":
        return _render(manager, is_admin_view, ("err", "Собес не из вашего пула."))
    db.manager_unassign_interview(iid)   # → back to the manager himself (responsible_id=mid)
    return _render(manager, is_admin_view, ("ok", "Собес возвращён вам."))


def _own_undistributed(manager: dict) -> list[dict]:
    """The manager's собесы he still attends himself (responsible_id == mid) — the set he can
    hand DOWN to the team. Under the auto-own model this replaces the old responsible-NULL pool."""
    mid = manager["id"]
    return [iv for iv in db.manager_interviews(mid) if iv.get("responsible_id") == mid]


@router.post("/manage/distribute", response_class=HTMLResponse)
def manage_distribute(as_: str = Form("", alias="as"),
                      me: dict = Depends(auth.current_responsible)):
    """One-tap balance: round-robin the manager's still-own собесы across himself + his active
    subordinates (time left unset — schedule later). Собесы landing back on himself are skipped
    (already his) so the notifier isn't needlessly re-armed."""
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)
    team = sorted(_allowed_interviewer_ids(manager))
    own = _own_undistributed(manager)
    if not own or len(team) <= 1:
        return _render(manager, is_admin_view,
                       ("err", "Нет своих нераспределённых собесов или сотрудников."))
    n = 0
    for i, iv in enumerate(own):
        rid = team[i % len(team)]
        if rid == manager["id"]:
            continue  # already his — leave it, don't re-arm the notifier
        try:
            db.manager_assign_interview(iv["id"], rid, None, None)
            n += 1
        except psycopg2.IntegrityError:
            continue
    return _render(manager, is_admin_view, ("ok", f"Роздано команде собесов: {n}."))


@router.post("/manage/distribute_to", response_class=HTMLResponse)
def manage_distribute_to(member_id: int = Form(...), count: int = Form(1),
                         gender: str = Form(""), direction: str = Form(""),
                         as_: str = Form("", alias="as"),
                         me: dict = Depends(auth.current_responsible)):
    """Hand N of the manager's OWN собесы (matching the gender/direction filter) DOWN to ONE
    chosen person — himself (keep) or a subordinate. Mirrors the admin split, scoped to this
    manager's own set + team (isolation: member must be him or his ACTIVE subordinate)."""
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)
    if member_id not in _allowed_interviewer_ids(manager):
        return _render(manager, is_admin_view,
                       ("err", "Можно раздавать только себе или своим сотрудникам."),
                       gender=gender, direction=direction)
    g = gender if gender in pool.GENDERS else None
    d = direction if direction in pool.DIRECTIONS else None
    interviews = pool.enrich_iv_rows(db.manager_interviews(manager["id"]))
    cands = [iv for iv in interviews
             if iv.get("responsible_id") == manager["id"] and pool._match(iv, None, g, d)]
    n = 0
    for iv in cands[:max(0, int(count))]:
        try:
            db.manager_assign_interview(iv["id"], member_id, None, None)
            n += 1
        except psycopg2.IntegrityError:
            continue
    who = db.get_responsible(member_id) or {}
    return _render(manager, is_admin_view,
                   ("ok", f"Роздано собесов: {n} → {who.get('name') or member_id}."),
                   gender=gender, direction=direction)


@router.post("/manage/reclaim_from", response_class=HTMLResponse)
def manage_reclaim_from(member_id: int = Form(...), count: int = Form(1),
                        gender: str = Form(""), direction: str = Form(""),
                        as_: str = Form("", alias="as"),
                        me: dict = Depends(auth.current_responsible)):
    """Pull N собесов (matching the gender/direction filter) back FROM a subordinate to the
    manager himself — the inverse of distribute_to. Symmetric with the admin↔manager reclaim.
    ISOLATION: the member must be one of his ACTIVE subordinates AND each собес's manager_id
    must be this manager (so a crafted POST can't reclaim another team's row)."""
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)
    sub_ids = {s["id"] for s in db.subordinates(manager["id"], active_only=True)}
    if member_id not in sub_ids:
        return _render(manager, is_admin_view,
                       ("err", "Забирать можно только у своих сотрудников."),
                       gender=gender, direction=direction)
    g = gender if gender in pool.GENDERS else None
    d = direction if direction in pool.DIRECTIONS else None
    held = pool.enrich_iv_rows(db.interviews_held_by(member_id, by="responsible"))
    cands = [iv for iv in held
             if iv.get("manager_id") == manager["id"] and iv.get("status") != "cancelled"
             and pool._match(iv, None, g, d)]
    n = 0
    for iv in cands[:max(0, int(count))]:
        db.manager_unassign_interview(iv["id"])   # → responsible_id=manager_id (back to him)
        n += 1
    who = db.get_responsible(member_id) or {}
    return _render(manager, is_admin_view,
                   ("ok", f"Забрано собесов: {n} ← {who.get('name') or member_id}."),
                   gender=gender, direction=direction)
