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
from html import escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from backend.interviews import auth, db, users_ui

router = APIRouter()


def _render_list(notice=None) -> HTMLResponse:
    users = db.list_responsibles(active_only=False)
    avail = {u["id"]: db.get_availability(u["id"]) for u in users}
    return HTMLResponse(users_ui.list_page(users, avail, notice))


def _render_edit(rid: int, notice=None) -> HTMLResponse:
    u = db.get_responsible(rid)
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    return HTMLResponse(users_ui.edit_page(u, db.get_availability(rid), notice))


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

    rows = []
    for d in range(7):
        enabled = form.get(f"en_{d}") is not None
        start = _to_min(form.get(f"start_{d}", ""))
        end = _to_min(form.get(f"end_{d}", ""))
        # overnight (end < start, crosses midnight) and start==end (a full 24h window)
        # are both valid — only reject an enabled day with unparseable times.
        if enabled and (start is None or end is None):
            return _render_edit(rid, ("err", f"{users_ui._DOW[d]}: укажите время начала и конца."))
        rows.append({"dow": d, "start_min": start or 0, "end_min": end or 0, "enabled": enabled})
    db.set_availability(rid, rows)
    return _render_edit(rid, ("ok", "Доступность сохранена."))
