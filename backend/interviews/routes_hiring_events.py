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

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from backend.tools import hiring_events

router = APIRouter()


@router.get("/hiring-events", response_class=HTMLResponse)
def hiring_events_page() -> HTMLResponse:
    """The live-hiring-events surface. Resolution is cache-first (only new invites hit
    the network), so repeat renders are fast."""
    return HTMLResponse(hiring_events.render_page())


@router.post("/hiring-events/refresh")
def hiring_events_refresh() -> JSONResponse:
    """Re-resolve every invite's Zoom link and refresh the cache; returns the counts.
    Useful after new invites land (the page itself resolves misses lazily too)."""
    groups = hiring_events.grouped_events(resolve=True)
    return JSONResponse({
        "rooms": len(groups),
        "invites": sum(len(g.get("invites") or []) for g in groups),
    })
