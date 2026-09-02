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
import re
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.interviews import auth, cabinet_ui, db, notify, slots
from backend.tools import mail_db, mailcrm

log = logging.getLogger("cabinet")

router = APIRouter(prefix="/cabinet")

_LINK_RE = re.compile(r'https?://[^\s"<>()\]}]+', re.I)
_LINK_SKIP_RE = re.compile(r"unsubscribe|/preferences|list-manage|/track|/pixel|utm_|beacon|/wf/open", re.I)


def _reply_links(msg: dict) -> list[str]:
    """Http(s) links from a message (plain + html) — shown to the interviewer when a thread can't
    be answered by email (a no-reply notification), so they can reach the recruiter via the
    scheduling/portal link instead. Drops tracking/unsubscribe noise; capped at 6."""
    text = f"{msg.get('plain') or ''} {msg.get('html') or ''}"
    seen: set[str] = set()
    out: list[str] = []
    for u in _LINK_RE.findall(text):
        u = u.rstrip('.,;:)"\'>')
        if not u or _LINK_SKIP_RE.search(u) or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= 6:
            break
    return out


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
    # MULTIPLE windows per weekday: each is a start_<d>/end_<d> input PAIR (getlist + zip);
    # a blank window is skipped, a day with no windows is off. overnight/24h stay valid.
    rows = []
    for d in range(7):
        starts, ends = form.getlist(f"start_{d}"), form.getlist(f"end_{d}")
        for s, e in zip(starts, ends):
            if not s or not e:
                continue
            rows.append({"dow": d, "start_min": _hhmm_to_min(s),
                         "end_min": _hhmm_to_min(e), "enabled": True})
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
    return HTMLResponse(cabinet_ui.thread_page(responsible, thread, hash=hash))


@router.post("/reply", response_class=HTMLResponse)
def reply(hash: str = Form(...), body: str = Form(...),
          responsible: dict = Depends(auth.current_responsible)):
    """An interviewer replies to a recruiter FROM the assigned persona's mailbox. Ownership
    guard identical to /thread: the thread must belong to one of THIS responsible's assigned
    personas, else 404. from/to/subject are derived server-side from the owned thread — the
    interviewer only supplies the body, so they cannot spoof sender or recipient."""
    row = None
    try:
        row = mail_db.get_row(hash)
    except Exception as e:
        log.warning("reply get_row failed: %s", e)
    if not row or row.get("mailbox") not in db.assigned_mailboxes(responsible["id"]):
        return HTMLResponse("<h1>404</h1>", status_code=404)

    thread = mailcrm.get_thread(hash, mark=False) or {}
    msgs = thread.get("messages") or []
    persona = row.get("mailbox") or ""
    # reply TO the latest INBOUND sender (the recruiter); derive subject + in-reply-to.
    inbound = [m for m in msgs if not m.get("outbound")]
    target = inbound[-1] if inbound else (msgs[-1] if msgs else {})
    to = (target.get("from_email") or "").strip()
    subj = (thread.get("subject") or target.get("subject") or "").strip()
    if subj and not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    mid = target.get("message_id") or ""

    sent = "err"
    links: list[str] = []
    if not (body or "").strip():
        sent = "err"
    elif mailcrm.is_undeliverable(to):
        # Greenhouse & co. notify FROM no-reply@…; a reply bounces (MAILER-DAEMON 550) and the
        # recruiter never sees it. Don't send silently — surface the message's own links so the
        # interviewer can reach the recruiter via the scheduling/portal link instead.
        sent = "noreply"
        links = _reply_links(target)
    else:
        try:
            res = mailcrm.send(from_email=persona, to=to, subject=subj or "Re:",
                               body=body, in_reply_to=mid)
            if res.get("noreply"):
                sent, links = "noreply", _reply_links(target)
            else:
                sent = "ok" if res.get("ok") else "err"
        except Exception as e:
            log.warning("cabinet reply send failed: %s", e)
            sent = "err"
    fresh = mailcrm.get_thread(hash, mark=False) or thread
    return HTMLResponse(cabinet_ui.thread_page(responsible, fresh, hash=hash, sent=sent, links=links))
