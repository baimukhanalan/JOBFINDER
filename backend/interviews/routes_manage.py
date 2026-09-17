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

from backend.interviews import auth, db, manage_ui, slots

router = APIRouter()

# `as` is a Python keyword, so the admin read-through's `?as=`/hidden field can't be a bare
# param name — bind it via an alias. Without the alias FastAPI looks for a field literally
# named `as_` and the admin read-through would silently lose its manager context.


def _acting(me: dict, as_id: str | int | None):
    """Resolve the manager whose portal is being acted on.

    Returns (manager_row, is_admin_view) or (None, is_admin_view). A manager always acts on
    himself (as_id ignored). An admin acts as the manager named by `as_id` (read-through);
    without a valid manager target an admin gets (None, True) → the caller redirects."""
    role = me.get("role")
    if role == "manager":
        return me, False
    if role == "admin":
        if as_id:
            try:
                m = db.get_responsible(int(as_id))
            except (TypeError, ValueError):
                m = None
            if m and m.get("role") == "manager":
                return m, True
        return None, True
    return None, False


def _render(manager: dict, is_admin_view: bool, notice=None) -> HTMLResponse:
    subs = db.subordinates(manager["id"], active_only=False)
    interviews = db.manager_interviews(manager["id"])
    loads = db.assigned_load([s["id"] for s in subs])
    # names for assigned rows: the manager + every subordinate (assignment is confined to them)
    names = {manager["id"]: manager.get("name") or manager.get("login") or "—"}
    for s in subs:
        names[s["id"]] = s.get("name") or s.get("login") or "—"
    return HTMLResponse(manage_ui.portal_page(
        manager, subs, interviews, loads, names, notice=notice,
        is_admin_view=is_admin_view))


def _allowed_interviewer_ids(manager: dict) -> set:
    """The manager himself + his ACTIVE subordinates — the only people he may assign to."""
    ids = {manager["id"]}
    ids |= {s["id"] for s in db.subordinates(manager["id"], active_only=True)}
    return ids


@router.get("/manage", response_class=HTMLResponse)
def manage_home(as_: str = Query("", alias="as"),
                me: dict = Depends(auth.current_responsible)):
    manager, is_admin_view = _acting(me, as_)
    if manager is None:
        # an admin with no (valid) target: send them to the roster where the managers +
        # their read-through links live.
        return RedirectResponse("/users", status_code=303)
    return _render(manager, is_admin_view)


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
    if not iv or iv.get("manager_id") != manager["id"]:
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
