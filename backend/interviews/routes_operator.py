"""FastAPI routes for the operator's «Собес» (interview-assign) modal in /mail.

Mounted on the dashboard app (`app.include_router(router)`). Endpoints:
  * GET  /mail/interview/grid   → the server-rendered week grid HTML fragment.
  * POST /mail/interview/assign → book a slot via `service.assign` (409 on conflict).
  * GET  /mail/interview/status → {assigned, responsible, start_ts} for a thread.

All time is GMT/UTC. This module owns no schema (see `backend.interviews.db`).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse

from backend.interviews import db, operator_ui, service, slots

router = APIRouter()


def _current_monday() -> date:
    # the grid axis is LOCAL (Almaty) time, so "this week" is the local week
    today = slots.to_local(datetime.now(timezone.utc)).date()
    return today - timedelta(days=today.weekday())


def _monday_of(monday: str) -> date:
    """Parse `monday` (ISO date), snap to that week's Monday; default = current week."""
    try:
        d = date.fromisoformat(monday) if monday else _current_monday()
    except ValueError:
        d = _current_monday()
    return d - timedelta(days=d.weekday())


@router.get("/mail/interview/grid")
def interview_grid(mailbox: str, monday: str = "", company: str | None = None,
                   jobid: str | None = None) -> HTMLResponse:
    # company/jobid absent on the FIRST render (resolved once via mailbox_context);
    # present on prev/next-week nav so grid_fragment skips the ~19k-file prefill glob.
    return HTMLResponse(
        operator_ui.grid_fragment(mailbox, _monday_of(monday), company, jobid))


@router.post("/mail/interview/assign")
def interview_assign(
    mailbox: str = Form(...),
    responsible_id: int = Form(...),
    start_iso: str = Form(...),
    company: str = Form(""),
    jobid: str = Form(""),
    thread_key: str = Form(""),
    source_message_hash: str = Form(""),
):
    try:
        service.assign(mailbox, responsible_id, start_iso, company, jobid,
                       thread_key, source_message_hash)
    except service.SlotConflict as exc:
        return HTMLResponse(f'<div class="iv-conflict">{escape(str(exc))}</div>',
                            status_code=409)
    except ValueError:
        # a malformed start_iso reaches service._parse_start → ValueError; answer 400,
        # not a 500. Neutral Russian, no stack detail.
        return HTMLResponse('<div class="iv-error">Неверный формат даты/времени</div>',
                            status_code=400)
    return HTMLResponse('<div class="iv-ok">Назначено</div>')


@router.get("/mail/interview/status")
def interview_status(mailbox: str, thread: str) -> JSONResponse:
    row = db.interview_for_thread(mailbox, thread)
    if not row:
        return JSONResponse({"assigned": False, "responsible": None, "start_ts": None})
    resp = None
    rid = row.get("responsible_id")
    if rid:
        r = db.get_responsible(rid)
        resp = r["name"] if r else None
    start = row.get("start_ts")
    return JSONResponse({
        "assigned": True,
        "responsible": resp,
        "start_ts": start.isoformat() if start else None,
    })
