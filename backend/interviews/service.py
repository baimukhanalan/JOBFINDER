"""Assignment service for the interview scheduler: the operator's week-grid of free
slots, and conflict-checked booking of an interview into a slot.

Sits on top of the DB layer (`backend.interviews.db`) and the pure slot logic
(`backend.interviews.slots`) — this module owns no schema and no time math of its
own beyond wiring the two together, plus a best-effort mailbox->(company, jobid)
lookup used to prefill the booking form from the mail CRM's prefill artifacts.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

from backend.interviews import db, slots

PREFILL_ROOT = Path(__file__).resolve().parents[2] / "uploads" / "prefill"


class SlotConflict(Exception):
    """Raised when the requested responsible/slot is not actually free to book."""


def grid_for_week(monday, viewer_tz: str | None = None) -> dict:
    """The operator's week grid of free slots starting at `monday`, drawn on the
    VIEWER's (operator's) timezone axis. Each free entry also carries the member's OWN
    local time for that slot.

    {"cells": {"YYYY-MM-DD:HH": [{"id","name","local","tz"}, ...]},
     "responsibles": [{"id","name"}], "hours": [0..23], "dates": [...],
     "viewer_tz": "Asia/Almaty", "viewer_label": "Almaty"}
    """
    viewer_tz = viewer_tz or slots.DEFAULT_TZ
    responsibles = db.list_responsibles(active_only=True)
    names_by_id = {r["id"]: r["name"] for r in responsibles}

    since = slots.cell_start_utc(viewer_tz, monday, 0)
    until = since + timedelta(days=7)

    per_resp = {}
    for r in responsibles:
        rid = r["id"]
        rtz = r.get("tz") or slots.DEFAULT_TZ
        # pad the booked window ±1 day so a cross-tz slot near a week edge is caught
        booked = db.booked_intervals(rid, since - timedelta(days=1), until + timedelta(days=1))
        per_resp[rid] = (db.get_availability(rid), rtz, booked)

    raw_cells = slots.free_grid(per_resp, monday, viewer_tz)
    cells = {
        key: [{"id": e["id"], "name": names_by_id.get(e["id"], ""),
               "local": e["local"], "tz": slots.tz_label(e["tz"])} for e in entries]
        for key, entries in raw_cells.items()
    }

    return {
        "cells": cells,
        "responsibles": [{"id": r["id"], "name": r["name"]} for r in responsibles],
        "hours": list(range(slots.HOUR_START, slots.HOUR_END)),
        "dates": [d.isoformat() for d in slots.week_dates(monday)],
        "viewer_tz": viewer_tz,
        "viewer_label": slots.tz_label(viewer_tz),
    }


def _parse_start(start_iso: str) -> datetime:
    dt = datetime.fromisoformat(start_iso)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def assign(mailbox: str, responsible_id: int, start_iso: str, company: str, jobid: str,
           thread_key: str, source_message_hash: str) -> dict:
    """Book an interview for `mailbox` with `responsible_id` at `start_iso`.

    Re-verifies the slot is actually free (availability window + no overlapping
    booking) right before inserting, and raises SlotConflict either on that
    re-check or on the DB's own double-book guard (a race between the check and
    the insert)."""
    start = _parse_start(start_iso)

    # slots.is_free only reasons about whole-hour cells [hour:00, hour:00+60min),
    # but db.insert_interview books [start, start+60min) using the UNROUNDED
    # start. A non-hour-aligned start (e.g. 10:45) would pass is_free (checked
    # against the 10:00-11:00 cell) yet insert 10:45-11:45, which can really
    # overlap an existing 11:00-12:00 booking — a double-book that slips past
    # both the is_free check and the DB's exact-start_ts unique guard. The
    # operator grid only ever submits :00 cell starts, so this is a defensive
    # guard keeping the checked window and the booked window identical.
    if start.second or start.microsecond:
        raise SlotConflict("interview start must be aligned to whole minutes")

    # is_free_at checks the EXACT [start, start+dur) window against the responsible's
    # availability (materialised in THEIR own timezone) + their bookings — so any
    # grid-submitted cell start (whole-hour in the operator's tz, whatever UTC minute
    # that lands on) is handled, and an overlapping booking can never slip through.
    resp = db.get_responsible(responsible_id)
    resp_tz = (resp or {}).get("tz") or slots.DEFAULT_TZ
    avail = db.get_availability(responsible_id)
    booked = db.booked_intervals(responsible_id, start - timedelta(days=1), start + timedelta(days=1))
    if not slots.is_free_at(avail, resp_tz, booked, start):
        raise SlotConflict(
            f"responsible {responsible_id} is not free at {start.isoformat()}")

    end = start + timedelta(minutes=slots.DURATION_MIN)
    try:
        interview_id = db.insert_interview(
            mailbox=mailbox, responsible_id=responsible_id, start_ts=start, end_ts=end,
            company=company, jobid=jobid, thread_key=thread_key,
            source_message_hash=source_message_hash,
        )
    except psycopg2.IntegrityError as exc:
        raise SlotConflict(
            f"slot already booked for responsible {responsible_id} at {start.isoformat()}"
        ) from exc

    row = db.interview_for_thread(mailbox, thread_key) if thread_key else None
    if row is not None and row.get("id") == interview_id:
        return row
    return {
        "id": interview_id,
        "mailbox": mailbox,
        "responsible_id": responsible_id,
        "start_ts": start,
        "end_ts": end,
        "company": company,
        "jobid": jobid,
        "thread_key": thread_key,
        "source_message_hash": source_message_hash,
        "status": "assigned",
    }


def mailbox_context(mailbox: str) -> dict:
    """Best-effort {"company","jobid"} for `mailbox`, from the newest
    uploads/prefill/<profile>/<jobid>/{persona.json,report.json} pair whose
    persona email matches. Never raises; {"company":"","jobid":""} if no match."""
    try:
        best_path = None
        best_mtime = -1.0
        for persona_path in PREFILL_ROOT.glob("*/*/persona.json"):
            try:
                data = json.loads(persona_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            email = (data.get("profile") or {}).get("email")
            if email != mailbox:
                continue
            try:
                mtime = persona_path.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_path = persona_path

        if best_path is None:
            return {"company": "", "jobid": ""}

        jobid = best_path.parent.name
        company = ""
        report_path = best_path.parent / "report.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                company = report.get("company") or ""
            except Exception:
                company = ""
        return {"company": company, "jobid": jobid}
    except Exception:
        return {"company": "", "jobid": ""}
