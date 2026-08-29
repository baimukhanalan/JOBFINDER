"""FastAPI routes for the operator «Пользователи» tab — CRUD over interview
responsibles (accounts assignable via «Собес»). Mounted on the dashboard app.

ADMIN-ONLY: none of these paths is on the dashboard's auth allowlist, so the
AdminAuthMiddleware redirects any non-admin request to /login before it reaches here.
POST handlers do the action then re-render the list/edit page with a result banner
(so a generated password is shown inline, never placed in a URL/redirect/log).
Availability is in each responsible's own timezone (iv_responsibles.tz).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from backend.interviews import auth, db, slots, users_ui

router = APIRouter()


def _render_list(notice=None) -> HTMLResponse:
    users = db.list_responsibles(active_only=False)
    avail = {u["id"]: db.get_availability(u["id"]) for u in users}
    # This week's booked interviews per responsible — the weekly load view, so the operator
    # can balance who gets the next собес. Week = Mon–Sun in the team default zone; each
    # interview is shown on each card in THAT interviewer's own timezone.
    now_local = slots.to_local(datetime.now(timezone.utc), slots.DEFAULT_TZ)
    monday = now_local.date() - timedelta(days=now_local.weekday())
    since = slots.cell_start_utc(slots.DEFAULT_TZ, monday, 0)
    until = since + timedelta(days=7)
    week_by_id: dict = {}
    sig = ""
    try:
        for iv in db.interviews_for_week(since, until):
            week_by_id.setdefault(iv["responsible_id"], []).append(iv)
        sig = db.week_signature(since, until)
    except Exception:
        week_by_id = {}
    return HTMLResponse(users_ui.list_page(users, avail, notice,
                                           week_by_id=week_by_id, monday=monday, week_sig=sig))


def _week_window():
    now_local = slots.to_local(datetime.now(timezone.utc), slots.DEFAULT_TZ)
    monday = now_local.date() - timedelta(days=now_local.weekday())
    since = slots.cell_start_utc(slots.DEFAULT_TZ, monday, 0)
    return since, since + timedelta(days=7)


@router.get("/users/signature")
def users_signature() -> JSONResponse:
    """A cheap signature of this week's interviews — the /users page polls it and, on a
    change (a собес assigned/reassigned/cancelled), re-fetches + swaps the cards in place."""
    try:
        since, until = _week_window()
        return JSONResponse({"sig": db.week_signature(since, until)})
    except Exception:
        return JSONResponse({"sig": ""})


def _render_edit(rid: int, notice=None) -> HTMLResponse:
    u = db.get_responsible(rid)
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    return HTMLResponse(users_ui.edit_page(u, db.get_availability(rid), notice,
                                           interview_count=db.interview_count(rid)))


@router.get("/users", response_class=HTMLResponse)
def users_list():
    return _render_list()


@router.post("/users/add", response_class=HTMLResponse)
def users_add(name: str = Form(...), login: str = Form(...),
              password: str = Form(""), role: str = Form("employee")):
    name, login = name.strip(), login.strip()
    role = role if role in ("admin", "employee") else "employee"
    if not name or not login:
        return _render_list(("err", "Имя и логин обязательны."))
    pw = password.strip() or secrets.token_urlsafe(9)
    try:
        # default to the team home zone; it auto-updates to their device zone on first
        # cabinet login (POST /cabinet/tz)
        db.add_responsible(login, auth.hash_password(pw), name, role=role, tz="Asia/Almaty")
    except Exception as e:
        return _render_list(("err", f"Не удалось создать (логин, возможно, занят): {escape(str(e))}"))
    return _render_list(("pw",
        f"Создан <b>{escape(name)}</b> (логин <b>{escape(login)}</b>). "
        f"Пароль: <code>{escape(pw)}</code> — сохрани, он больше не покажется."))


@router.get("/users/{rid}", response_class=HTMLResponse)
def users_edit(rid: int):
    return _render_edit(rid)


@router.post("/users/{rid}/passwd", response_class=HTMLResponse)
def users_passwd(rid: int, password: str = Form("")):
    if not db.get_responsible(rid):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    pw = password.strip() or secrets.token_urlsafe(9)
    db.set_password_hash(rid, auth.hash_password(pw))
    return _render_edit(rid, ("pw", f"Новый пароль: <code>{escape(pw)}</code> — сохрани, больше не покажу."))


@router.post("/users/{rid}/role", response_class=HTMLResponse)
def users_role(rid: int, role: str = Form(...)):
    if not db.get_responsible(rid):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    if role not in ("admin", "employee"):
        return _render_edit(rid, ("err", "Неизвестная роль."))
    db.set_role(rid, role)
    return _render_edit(rid, ("ok", f"Роль изменена на «{'админ' if role == 'admin' else 'интервьюер'}»."))


@router.post("/users/{rid}/telegram", response_class=HTMLResponse)
def users_telegram(rid: int, chat_id: str = Form("")):
    if not db.get_responsible(rid):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    chat_id = chat_id.strip()
    if not chat_id:
        db.set_telegram_chat(rid, None)
        return _render_edit(rid, ("ok", "Telegram отвязан."))
    try:
        db.set_telegram_chat(rid, int(chat_id))
    except ValueError:
        return _render_edit(rid, ("err", "chat_id должен быть числом."))
    return _render_edit(rid, ("ok", "Telegram сохранён."))


@router.post("/users/{rid}/active", response_class=HTMLResponse)
def users_active(rid: int, active: str = Form(...)):
    if not db.get_responsible(rid):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    on = active == "1"
    db.set_active(rid, on)
    return _render_edit(rid, ("ok", "Пользователь включён." if on else "Пользователь отключён (сессия отозвана)."))


@router.post("/users/{rid}/delete", response_class=HTMLResponse)
def users_delete(rid: int, me: dict = Depends(auth.current_responsible)):
    u = db.get_responsible(rid)
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    # Never let an admin delete the account they are signed in as (would lock themselves out).
    if me and me.get("id") == rid:
        return _render_edit(rid, ("err", "Нельзя удалить собственную учётную запись — вы под ней вошли."))
    # The iv_interviews FK has no ON DELETE, so a user with any interview can't be hard-deleted
    # (history is kept). Guide the operator to deactivate instead.
    n = db.interview_count(rid)
    if n:
        return _render_edit(rid, ("err",
            f"Нельзя удалить: за пользователем закреплено интервью — {n}. "
            "Чтобы сохранить историю, отключите его (кнопка «Отключить») вместо удаления."))
    try:
        db.delete_responsible(rid)
    except Exception as e:
        return _render_edit(rid, ("err", f"Не удалось удалить: {escape(str(e))}"))
    return _render_list(("ok", f"Пользователь «{escape(str(u.get('name') or u.get('login') or rid))}» удалён."))


@router.post("/users/{rid}/availability", response_class=HTMLResponse)
async def users_availability(rid: int, request: Request):
    if not db.get_responsible(rid):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    form = await request.form()

    def _to_min(v: str) -> int | None:
        try:
            hh, mm = str(v).split(":")
            return int(hh) * 60 + int(mm)
        except Exception:
            return None

    # MULTIPLE windows per weekday: each window is a start_<d>/end_<d> input PAIR, read with
    # getlist and zipped. A blank/incomplete window is skipped; a day with no windows is off.
    # overnight (end<start) and 24h (start==end) windows stay valid — never rejected.
    rows = []
    for d in range(7):
        starts, ends = form.getlist(f"start_{d}"), form.getlist(f"end_{d}")
        for s, e in zip(starts, ends):
            sm, em = _to_min(s), _to_min(e)
            if sm is None or em is None:
                continue
            rows.append({"dow": d, "start_min": sm, "end_min": em, "enabled": True})
    db.set_availability(rid, rows)
    return _render_edit(rid, ("ok", "Доступность сохранена."))
