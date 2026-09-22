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

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.interviews import auth, cabinet_ui, db, notify, slots
from backend.tools import mail_db, mailcrm

log = logging.getLogger("cabinet")

router = APIRouter(prefix="/cabinet")


def _acting_cabinet(me: dict, as_id: str | int | None) -> dict:
    """Resolve WHOSE cabinet a request acts on (admin read-through — mirrors
    `routes_manage._acting`).

    An ADMIN who passes a valid `?as=<id>` acts INSIDE that user's cabinet: sees their
    schedule / inbox / threads and may save availability or reply FOR them (all the
    ownership guards below then key on the TARGET). Everyone else — and an admin whose
    `?as` is blank/invalid — acts on their OWN cabinet. A non-admin's `?as` is IGNORED, so
    a manager/employee can NEVER spoof it to view or act as someone else."""
    if as_id and db.has_role(me, "admin"):
        try:
            target = db.get_responsible(int(as_id))
        except (TypeError, ValueError):
            target = None
        if target:
            return target
    return me


def _view_as(me: dict, responsible: dict):
    """The `as_id` to thread into links/forms so the admin read-through survives navigation —
    the target id when acting as someone else, else None (a normal self-view)."""
    return responsible["id"] if responsible["id"] != me["id"] else None

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


def _not_found() -> HTMLResponse:
    """A small styled cabinet-shell 404 (reuses cabinet_ui._doc) instead of a bare `<h1>404</h1>`.
    Reachable by a legit interviewer via a stale/reassigned link (a thread they no longer own),
    so it must look like the rest of the cabinet and offer a way back to their inbox."""
    body = (
        '<div style="max-width:520px;margin:56px auto 0;text-align:center">'
        '<div style="font-size:44px;font-weight:800;letter-spacing:-.02em;color:var(--ink)">404</div>'
        '<p style="color:var(--ink-soft);font-size:14px;line-height:1.55;margin:8px 0 20px">'
        'Переписка недоступна — возможно, собеседование переназначено или ссылка устарела.</p>'
        '<a class="hbtn" href="/cabinet/inbox">← К списку</a></div>')
    return HTMLResponse(cabinet_ui._doc(body, "Не найдено"), status_code=404)


@router.get("", response_class=HTMLResponse)
def dashboard(as_: str = Query("", alias="as"),
              me: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    responsible = _acting_cabinet(me, as_)
    interviews = db.interviews_for_responsible(responsible["id"], upcoming_only=True)
    return HTMLResponse(cabinet_ui.dashboard_page(responsible, interviews,
                                                  as_id=_view_as(me, responsible)))


@router.get("/availability", response_class=HTMLResponse)
def availability_get(as_: str = Query("", alias="as"),
                     me: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    responsible = _acting_cabinet(me, as_)
    rows = db.get_availability(responsible["id"])
    return HTMLResponse(cabinet_ui.availability_page(responsible, rows,
                                                     as_id=_view_as(me, responsible)))


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
                            me: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    form = await request.form()
    responsible = _acting_cabinet(me, form.get("as"))
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
    return HTMLResponse(cabinet_ui.availability_page(responsible, rows, saved=True,
                                                     as_id=_view_as(me, responsible)))


@router.get("/inbox", response_class=HTMLResponse)
def inbox(as_: str = Query("", alias="as"),
          me: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    responsible = _acting_cabinet(me, as_)
    rows: list[dict] = []
    for m in sorted(db.assigned_mailboxes(responsible["id"])):
        try:
            rows.extend(mailcrm.list_messages(mailbox=m, limit=100))
        except Exception as e:
            log.warning("list_messages failed for %s: %s", m, e)
    rows.sort(key=lambda r: r.get("date_ts", 0), reverse=True)
    # Strip leaked CSS/HTML from the preview snippet, same as the operator grouped inbox does
    # (a Calendly reminder otherwise shows `a:visited{color…}@media…` garbage to the employee).
    try:
        from backend.tools.candidates_inbox import _clean_snippet
        for r in rows:
            if r.get("snippet"):
                r["snippet"] = _clean_snippet(r["snippet"])
    except Exception as e:
        log.warning("snippet clean failed: %s", e)
    return HTMLResponse(cabinet_ui.inbox_page(responsible, rows,
                                              as_id=_view_as(me, responsible)))


@router.get("/thread", response_class=HTMLResponse)
def thread(hash: str, as_: str = Query("", alias="as"),
           me: dict = Depends(auth.current_responsible)):
    responsible = _acting_cabinet(me, as_)
    # OWNERSHIP GUARD (security core): resolve the row first, verify its mailbox is
    # assigned to THIS responsible, and only then read the thread. Any miss → 404.
    row = None
    try:
        row = mail_db.get_row(hash)
    except Exception as e:
        log.warning("get_row failed: %s", e)
    if not row or row.get("mailbox") not in db.assigned_mailboxes(responsible["id"]):
        return _not_found()
    # READ-ONLY: mark=False so opening a thread never flips the persona's messages to
    # seen (which would also move them in the OPERATOR's inbox).
    thread = mailcrm.get_thread(hash, mark=False)
    if not thread:
        return _not_found()
    return HTMLResponse(cabinet_ui.thread_page(responsible, thread, hash=hash,
                                               as_id=_view_as(me, responsible)))


@router.post("/reply", response_class=HTMLResponse)
def reply(hash: str = Form(...), body: str = Form(...), as_: str = Form("", alias="as"),
          me: dict = Depends(auth.current_responsible)):
    responsible = _acting_cabinet(me, as_)
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
        return _not_found()

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
    return HTMLResponse(cabinet_ui.thread_page(responsible, fresh, hash=hash, sent=sent,
                                               links=links, as_id=_view_as(me, responsible)))
