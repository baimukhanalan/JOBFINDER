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
    # enrich with gender/direction AND the priority signals (booking deadline, salary, expired)
    # so BOTH the «Пул на распределение» filter and the new priority + «актуальные предстоящие»
    # cards read the same urgency/priority as the «Собес» screen.
    interviews = pool.enrich_priority(db.manager_interviews(mid))
    pool_all = [iv for iv in interviews if not iv.get("responsible_id")]
    own = [iv for iv in interviews if iv.get("responsible_id") == mid]
    team_ivs = [iv for iv in interviews if iv.get("responsible_id") in sub_ids]
    g = gender if gender in pool.GENDERS else None
    d = direction if direction in pool.DIRECTIONS else None
    pool_ivs = [iv for iv in pool_all if pool._match(iv, (q or None), g, d)]
    loads = db.assigned_load([s["id"] for s in subs])
    # names for assigned rows: the manager + every subordinate (assignment is confined to them)
    names = {mid: manager.get("name") or manager.get("login") or "—"}
    for s in subs:
        names[s["id"]] = s.get("name") or s.get("login") or "—"
    counts = {"pool": len(pool_all), "pool_shown": len(pool_ivs),
              "filtered": bool((q or "").strip() or g or d),
              "own": len(own), "team": len(team_ivs),
              "team_size": len([s for s in subs if s.get("active")])}
    # sort_base = this /manage URL WITH its current query minus pool_sort, so the priority-card
    # sort toggle keeps the admin read-through (?as) + the pool filter (?q/&gender/&direction).
    from urllib.parse import urlencode
    qs = {k: v for k, v in (("as", mid if is_admin_view else ""), ("q", q),
                            ("gender", gender), ("direction", direction)) if v}
    sort_base = "/manage" + ("?" + urlencode(qs) if qs else "")
    return HTMLResponse(manage_ui.portal_page(
        manager, subs, pool_ivs, own, team_ivs, loads, names, counts,
        q=q, gender=gender, direction=direction, notice=notice,
        is_admin_view=is_admin_view, pool_all=pool_all, scope_ivs=interviews,
        pool_sort=pool_sort, sort_base=sort_base))


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
    db.manager_unassign_interview(iid)
    return _render(manager, is_admin_view, ("ok", "Собес возвращён в пул."))


@router.post("/manage/distribute", response_class=HTMLResponse)
def manage_distribute(as_: str = Form("", alias="as"),
                      me: dict = Depends(auth.current_responsible)):
    """Round-robin every UNASSIGNED interview in this manager's pool across himself + his
    active subordinates (time left unset — schedule later). A one-tap balance tool."""
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        return RedirectResponse("/users", status_code=303)
    team = sorted(_allowed_interviewer_ids(manager))
    pool_ivs = [iv for iv in db.manager_interviews(manager["id"]) if not iv.get("responsible_id")]
    if not pool_ivs or not team:
        return _render(manager, is_admin_view, ("err", "Нет непризначенных собесов или команды."))
    n = 0
    for i, iv in enumerate(pool_ivs):
        rid = team[i % len(team)]
        try:
            db.manager_assign_interview(iv["id"], rid, None, None)
            n += 1
        except psycopg2.IntegrityError:
            continue
    return _render(manager, is_admin_view, ("ok", f"Распределено собесов: {n}."))


@router.post("/manage/distribute_to", response_class=HTMLResponse)
def manage_distribute_to(member_id: int = Form(...), count: int = Form(1),
                         gender: str = Form(""), direction: str = Form(""),
                         as_: str = Form("", alias="as"),
                         me: dict = Depends(auth.current_responsible)):
    """Give N interviews from the manager's pool (matching the gender/direction filter) to ONE
    chosen person — himself or a subordinate. Mirrors the admin split, scoped to this manager's
    pool + team (isolation: member must be him or his subordinate)."""
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
             if not iv.get("responsible_id") and pool._match(iv, None, g, d)]
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
