"""Route for the «События найма» operator surface (live Zoom hiring events).

Mounted on the dashboard app (guarded include, like the other interview routers). The
page is admin-gated by the AdminAuthMiddleware (it is not on the public allowlist), so
only a logged-in operator sees it. The heavy lifting (reading the invites out of
``mail_index``, resolving the Zoom links, rendering) lives in
``backend.tools.hiring_events`` — this module is only the HTTP entry point.

These events are DELIBERATELY separate from the «Собес» interview pool: they are TP
mass-invites (mail kind='other'), captured directly by sender+subject, never written
into ``iv_interviews``, so the interview pool stays clean.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)

from backend.interviews import auth
from backend.tools import hiring_events

router = APIRouter()


def _parse_join(raw: str, me: dict | None):
    """Parse a claim's join time (a `HH:MM` <input type=time>, or a full
    `YYYY-MM-DDTHH:MM`) as wall-clock in the acting user's timezone → tz-aware UTC. A bare
    `HH:MM` is anchored to TODAY in the user's zone. None on anything empty/unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from backend.interviews import slots
        tz = (me or {}).get("tz")
        if "T" in raw:
            naive = datetime.strptime(raw[:16], "%Y-%m-%dT%H:%M")
        else:
            t = datetime.strptime(raw[:5], "%H:%M").time()
            today = slots.to_local(datetime.now(slots.UTC), tz).date()
            naive = datetime.combine(today, t)
        return naive.replace(tzinfo=slots.zone(tz)).astimezone(slots.UTC)
    except Exception:
        return None


@router.get("/hiring-events", response_class=HTMLResponse)
def hiring_events_page(ctx: str = "",
                       me: dict = Depends(auth.current_responsible)) -> HTMLResponse:
    """The live-hiring-events surface, SHARED across all roles. Resolution is cache-first
    (only new invites hit the network), so repeat renders are fast. Every claim («кто и во
    сколько подключится к кандидату») is loaded + shown to everyone; the acting user gets the
    inline claim control."""
    groups = hiring_events.grouped_events()
    mailboxes = [inv.get("mailbox")
                 for g in groups for inv in (g.get("invites") or [])]
    claims = {}
    color_for = None
    try:
        from backend.interviews import db
        claims = db.event_claims_for(mailboxes)
        color_for = db.color_for
    except Exception:
        claims, color_for = {}, None
    return HTMLResponse(hiring_events.render_page(
        groups, claims=claims, me=me, color_for=color_for, ctx=ctx))


@router.post("/hiring-events/claim")
def hiring_events_claim(mailbox: str = Form(""), join_local: str = Form(""),
                        joined: str = Form(""),
                        me: dict = Depends(auth.current_responsible)):
    """Record the ACTING user's claim on a candidate («я подключусь в X:XX» / «уже подключился»).
    Always keyed to the acting user (no ?as spoofing). Redirects back to the board."""
    mbx = (mailbox or "").strip()
    if mbx:
        join_ts = _parse_join(join_local, me)
        is_joined = (joined or "").strip().lower() in ("1", "true", "on", "yes")
        try:
            from backend.interviews import db
            db.set_event_claim(mbx, me["id"], join_ts=join_ts, joined=is_joined)
        except Exception:
            pass
    return RedirectResponse("/hiring-events", status_code=303)


@router.post("/hiring-events/unclaim")
def hiring_events_unclaim(mailbox: str = Form(""),
                          me: dict = Depends(auth.current_responsible)):
    """Remove the acting user's own claim on a candidate («Отменить»)."""
    mbx = (mailbox or "").strip()
    if mbx:
        try:
            from backend.interviews import db
            db.clear_event_claim(mbx, me["id"])
        except Exception:
            pass
    return RedirectResponse("/hiring-events", status_code=303)


@router.get("/hiring-events/resume")
def hiring_events_resume(mbx: str = ""):
    """Stream a hiring-event candidate's résumé PDF (admin-gated, like the page itself).
    ``mbx`` is the persona mailbox. Serves the persona's generated ``resume.pdf``, falling
    back to a fresh render from its persona.json; 404 when neither is available (the page
    hides the button in that case). Filename = the candidate's name."""
    mbx = (mbx or "").strip()
    persona = hiring_events.load_persona(mbx)
    fname = hiring_events.resume_filename(mbx, persona)
    path = hiring_events.resume_pdf_path(mbx)
    if path:
        return FileResponse(path, media_type="application/pdf", filename=fname)
    if persona:
        resume = ((persona.get("profile") or {}).get("resume")
                  or persona.get("resume") or {})
        try:
            from backend.tools import drafts_ui
            pdf = drafts_ui.render_resume_pdf(resume)
        except Exception:
            pdf = None
        if pdf:
            return Response(content=pdf, media_type="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    return JSONResponse({"error": "not found"}, status_code=404)


@router.post("/hiring-events/refresh")
def hiring_events_refresh() -> JSONResponse:
    """Re-resolve every invite's Zoom link and refresh the cache; returns the counts.
    Useful after new invites land (the page itself resolves misses lazily too)."""
    groups = hiring_events.grouped_events(resolve=True)
    return JSONResponse({
        "rooms": len(groups),
        "invites": sum(len(g.get("invites") or []) for g in groups),
    })
