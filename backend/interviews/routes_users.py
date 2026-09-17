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

from backend.interviews import auth, db, pool, slots, users_ui

router = APIRouter()

_ROLES = ("admin", "manager", "employee")


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
    # manager-tier context for the delegation tools (split the pool, send one to a manager,
    # read-through into a manager's portal). All best-effort so a hiccup never breaks /users.
    managers: list[dict] = []
    pool_count = 0
    pool_rows: list[dict] = []
    pool_facets: dict = {}
    mgr_alloc: dict = {}
    try:
        managers = db.list_managers(active_only=True)
        if managers:
            pool_facets = pool.facets()
            pool_count = pool_facets.get("total", 0)
            pool_rows = pool.unallocated(limit=400)   # for the email-search datalist
            for m in managers:
                ivs = db.manager_interviews(m["id"])
                mgr_alloc[m["id"]] = {
                    "total": len(ivs),
                    "assigned": sum(1 for iv in ivs if iv.get("responsible_id")),
                }
    except Exception:
        managers = managers or []
    return HTMLResponse(users_ui.list_page(
        users, avail, notice, week_by_id=week_by_id, monday=monday, week_sig=sig,
        managers=managers, pool_count=pool_count, pool_rows=pool_rows,
        pool_facets=pool_facets, mgr_alloc=mgr_alloc))


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
    managers = []
    try:
        managers = [m for m in db.list_managers(active_only=False) if m["id"] != rid]
    except Exception:
        managers = []
    return HTMLResponse(users_ui.edit_page(u, db.get_availability(rid), notice,
                                           interview_count=db.interview_count(rid),
                                           managers=managers))


@router.get("/users", response_class=HTMLResponse)
def users_list():
    return _render_list()


@router.post("/users/add", response_class=HTMLResponse)
async def users_add(request: Request):
    # MULTI-ROLE: the add form sends 0..N `role` checkboxes; default to a single interviewer.
    form = await request.form()
    name = (form.get("name") or "").strip()
    login = (form.get("login") or "").strip()
    password = (form.get("password") or "").strip()
    roles = db.normalize_roles([r for r in form.getlist("role") if r in _ROLES])
    if not name or not login:
        return _render_list(("err", "Имя и логин обязательны."))
    # a subordinate under a manager (only meaningful when they hold the interviewer role)
    mid = None
    if "employee" in roles and (form.get("manager_id") or "").strip():
        try:
            mid = int(form.get("manager_id"))
        except (ValueError, TypeError):
            mid = None
    pw = password or secrets.token_urlsafe(9)
    try:
        # default to the team home zone; it auto-updates to their device zone on first
        # cabinet login (POST /cabinet/tz)
        db.add_responsible(login, auth.hash_password(pw), name, roles=roles,
                           tz="Asia/Almaty", manager_id=mid)
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


_ROLE_LBL = {"admin": "админ", "manager": "управляющий", "employee": "интервьюер"}


@router.post("/users/{rid}/roles", response_class=HTMLResponse)
async def users_roles(rid: int, request: Request):
    """Set a user's MULTI-ROLE set from checkboxes (admin/manager/employee). Persisted
    immediately. Used by BOTH the inline list editor and the edit page. `from_list=1` (a
    hidden field the list editor sends) re-renders the whole list so the change shows in
    place; otherwise the edit page is re-rendered."""
    u = db.get_responsible(rid)
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    form = await request.form()
    roles = db.normalize_roles([r for r in form.getlist("role") if r in _ROLES])
    db.set_roles(rid, roles)
    # someone who no longer holds the interviewer role can't be a subordinate — clear a stale
    # manager link (an admin/manager-only user is nobody's report)
    if "employee" not in roles:
        try:
            db.set_manager(rid, None)
        except Exception:
            pass
    lbls = ", ".join(_ROLE_LBL.get(r, r) for r in roles)
    notice = ("ok", f"Роли обновлены: {lbls}.")
    if (form.get("from_list") or "") == "1":
        return _render_list(notice)
    return _render_edit(rid, notice)


@router.post("/users/{rid}/manager", response_class=HTMLResponse)
def users_set_manager(rid: int, manager_id: str = Form("")):
    """Set (or clear) which manager supervises this responsible. Admin-only (gated). A
    manager can't supervise themselves, and only a real manager account may be chosen."""
    u = db.get_responsible(rid)
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    mid = manager_id.strip()
    if not mid:
        db.set_manager(rid, None)
        return _render_edit(rid, ("ok", "Управляющий откреплён."))
    try:
        mid_i = int(mid)
    except ValueError:
        return _render_edit(rid, ("err", "Неверный управляющий."))
    if mid_i == rid:
        return _render_edit(rid, ("err", "Нельзя назначить сотрудника управляющим самому себе."))
    m = db.get_responsible(mid_i)
    if not m or not db.has_role(m, "manager"):
        return _render_edit(rid, ("err", "Выбранный пользователь не является управляющим."))
    db.set_manager(rid, mid_i)
    return _render_edit(rid, ("ok", f"Закреплён за управляющим «{escape(m.get('name') or '')}»."))


@router.post("/users/allocate/split", response_class=HTMLResponse)
async def users_allocate_split(request: Request):
    """Divide the free interview pool among managers — the «Разделить интервью» tool. Each
    manager gets a count (equal or custom); blocks are taken newest-first. Form fields:
    `count_<manager_id>` = how many to allocate to that manager (blank/0 = none)."""
    form = await request.form()
    managers = {m["id"] for m in db.list_managers(active_only=True)}
    counts: dict[int, int] = {}
    for key in form.keys():
        if not key.startswith("count_"):
            continue
        try:
            mid = int(key[len("count_"):])
            n = int((form.get(key) or "0").strip() or 0)
        except ValueError:
            continue
        if mid in managers and n > 0:
            counts[mid] = n
    if not counts:
        return _render_list(("err", "Укажите, сколько интервью выделить хотя бы одному управляющему."))
    gender = (form.get("split_gender") or "").strip() or None
    direction = (form.get("split_direction") or "").strip() or None
    if gender not in (None,) + pool.GENDERS:
        gender = None
    if direction not in (None,) + pool.DIRECTIONS:
        direction = None
    try:
        allocated = pool.split(counts, gender=gender, direction=direction)
    except Exception as e:
        return _render_list(("err", f"Не удалось распределить: {escape(str(e))}"))
    total = sum(allocated.values())
    if not total:
        return _render_list(("err", "По этому фильтру в свободном пуле нет интервью для распределения."))
    parts = []
    for mid, n in allocated.items():
        m = db.get_responsible(mid)
        parts.append(f"{escape((m or {}).get('name') or str(mid))}: {n}")
    filt = []
    if gender:
        filt.append("муж." if gender == "male" else "жен.")
    if direction:
        filt.append({"it": "IT", "nonit": "не-IT", "other": "другое"}.get(direction, direction))
    fs = f" ({', '.join(filt)})" if filt else ""
    return _render_list(("ok", f"Выделено интервью — {total}{fs}. " + "; ".join(parts) + "."))


@router.post("/users/allocate/send", response_class=HTMLResponse)
def users_allocate_send(mailbox: str = Form(...), manager_id: int = Form(...)):
    """Send ONE specific pool interview (a persona mailbox) to a specific manager."""
    m = db.get_responsible(manager_id)
    if not m or not db.has_role(m, "manager"):
        return _render_list(("err", "Выберите управляющего."))
    ok = False
    try:
        ok = pool.allocate_specific(mailbox.strip(), manager_id)
    except Exception as e:
        return _render_list(("err", f"Не удалось выделить: {escape(str(e))}"))
    if not ok:
        return _render_list(("err", "Это интервью уже выделено или недоступно."))
    return _render_list(("ok",
        f"Интервью «{escape(mailbox)}» выделено управляющему «{escape(m.get('name') or '')}»."))


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


# The three REAL interviewers (Alan/Аружан/Нурбол) are protected from deletion — their logins
# are literally "1"/"2"/"3". Do NOT delete them (see CLAUDE.md).
_PROTECTED_LOGINS = {"1", "2", "3"}


@router.post("/users/{rid}/delete", response_class=HTMLResponse)
def users_delete(rid: int, me: dict = Depends(auth.current_responsible)):
    u = db.get_responsible(rid)
    if not u:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    # Never let an admin delete the account they are signed in as (would lock themselves out).
    if me and me.get("id") == rid:
        return _render_edit(rid, ("err", "Нельзя удалить собственную учётную запись — вы под ней вошли."))
    # Protect the real interviewers 1/2/3.
    if (u.get("login") or "") in _PROTECTED_LOGINS:
        return _render_edit(rid, ("err",
            "Этого пользователя удалять нельзя (штатный интервьюер). Можно только отключить."))
    # Hard-delete ANY user (incl. deactivated / with interview history): the cascade returns
    # their managed/assigned interviews to the pool and clears every FK before removing the row.
    try:
        db.delete_responsible_cascade(rid)
    except Exception as e:
        return _render_edit(rid, ("err", f"Не удалось удалить: {escape(str(e))}"))
    return _render_list(("ok",
        f"Пользователь «{escape(str(u.get('name') or u.get('login') or rid))}» удалён. "
        "Его собеседования (если были) возвращены в пул."))


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
