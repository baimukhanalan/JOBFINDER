"""Responsible cabinet, mounted on the MAIN operator dashboard under `/cabinet`.

Merged into `jobs.systeam.kz` (2026-08-29): one domain, one login. An employee
(`role='employee'`) is confined by the dashboard's AdminAuthMiddleware to `/cabinet/*`
(whitelist) — everything else is blocked, so this router is the employee's entire
world. Admins may also view it. Every view is READ-ONLY except the responsible editing
their OWN weekly availability.

Security core — the ownership guard (`GET /cabinet/thread`): a responsible must NEVER
read another responsible's mail. We resolve the message row and verify its mailbox is in
THIS responsible's assigned set (via a booked, non-cancelled `iv_interviews` row);
otherwise 404. The shared `mailcrm.get_thread` is left unchanged — the guard lives here.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.interviews import auth, cabinet_ui, db, notify, slots
from backend.tools import mail_db, mailcrm

log = logging.getLogger("cabinet")

router = APIRouter(prefix="/cabinet")


@router.get("", response_class=HTMLResponse)
def dashboard(responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    interviews = db.interviews_for_responsible(responsible["id"], upcoming_only=True)
    return HTMLResponse(cabinet_ui.dashboard_page(responsible, interviews))


@router.get("/availability", response_class=HTMLResponse)
def availability_get(responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    rows = db.get_availability(responsible["id"])
    return HTMLResponse(cabinet_ui.availability_page(responsible, rows))


@router.post("/tg/connect")
def tg_connect(responsible: dict = Depends(auth.current_responsible)):
    """Start self-service Telegram linking: mint a one-time code and redirect to the
    bot's deep link. When the interviewer presses Start there, the notifier's
    poll_updates binds their chat_id (see notify.poll_updates)."""
    code = secrets.token_urlsafe(8)
    db.set_tg_link_code(responsible["id"], code)
    uname = notify.bot_username()
    if not uname:
        return RedirectResponse("/cabinet/availability?tgerr=1", status_code=303)
    return RedirectResponse(f"https://t.me/{uname}?start={code}", status_code=303)


@router.post("/tg/unlink")
def tg_unlink(responsible: dict = Depends(auth.current_responsible)):
    db.set_telegram_chat(responsible["id"], None)
    return RedirectResponse("/cabinet/availability", status_code=303)


@router.post("/tz", response_class=HTMLResponse)
async def set_tz(request: Request,
                 responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    """Adopt the responsible's device timezone (auto-detected in the cabinet). Only a
    real IANA name is accepted (slots.zone falls back to the default on garbage)."""
    form = await request.form()
    tz = (form.get("tz") or "").strip()
    if tz and slots.zone(tz).key == tz and tz != responsible.get("tz"):
        db.set_tz(responsible["id"], tz)
    return HTMLResponse("ok")


def _hhmm_to_min(s: str) -> int:
    try:
        h, m = (s or "").split(":")[:2]
        return max(0, min(24 * 60, int(h) * 60 + int(m)))
    except (ValueError, TypeError):
        return 0


@router.post("/availability", response_class=HTMLResponse)
async def availability_post(request: Request,
                            responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    form = await request.form()
    rows = []
    for d in range(7):
        rows.append({
            "dow": d,
            "start_min": _hhmm_to_min(form.get(f"start_{d}", "")),
            "end_min": _hhmm_to_min(form.get(f"end_{d}", "")),
            "enabled": form.get(f"enabled_{d}") is not None,
        })
    db.set_availability(responsible["id"], rows)
    rows = db.get_availability(responsible["id"])
    return HTMLResponse(cabinet_ui.availability_page(responsible, rows, saved=True))


@router.get("/inbox", response_class=HTMLResponse)
def inbox(responsible: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    rows: list[dict] = []
    for m in sorted(db.assigned_mailboxes(responsible["id"])):
        try:
            rows.extend(mailcrm.list_messages(mailbox=m, limit=100))
        except Exception as e:
            log.warning("list_messages failed for %s: %s", m, e)
    rows.sort(key=lambda r: r.get("date_ts", 0), reverse=True)
    return HTMLResponse(cabinet_ui.inbox_page(responsible, rows))


@router.get("/thread", response_class=HTMLResponse)
def thread(hash: str, responsible: dict = Depends(auth.current_responsible)):
    # OWNERSHIP GUARD (security core): resolve the row first, verify its mailbox is
    # assigned to THIS responsible, and only then read the thread. Any miss → 404.
    row = None
    try:
        row = mail_db.get_row(hash)
    except Exception as e:
        log.warning("get_row failed: %s", e)
    if not row or row.get("mailbox") not in db.assigned_mailboxes(responsible["id"]):
        return HTMLResponse("<h1>404</h1>", status_code=404)
    # READ-ONLY: mark=False so opening a thread never flips the persona's messages to
    # seen (which would also move them in the OPERATOR's inbox).
    thread = mailcrm.get_thread(hash, mark=False)
    if not thread:
        return HTMLResponse("<h1>404</h1>", status_code=404)
    return HTMLResponse(cabinet_ui.thread_page(responsible, thread))
