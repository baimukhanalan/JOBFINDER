"""ISOLATED daily auto-assign + evening-slot reservation for ONE interviewer (Alan Bai, id 1035).

The owner's directive: every day around 20:00 (±1.5h, i.e. the 18:30–21:30 evening window in the
interviewer's own tz) Alan should have interviews QUEUED and TIMED, with zero manual clicking, so the
«Interview Cockpit» prep engine + the human just show up. This module is the reliable DB core of that:

  1. ASSIGN  — keep Alan supplied: if his unscheduled queue is low, auto-own ONE fresh pool interview
     to him (reuse `pool.allocate_specific`), PREFERRING a persona whose résumé still exists on disk so
     the prep pack can actually load it into Natively (résumé-pruned backlog собесы are deprioritised).
  2. RESERVE — for each of his non-cancelled собесы with no start_ts, pick the next FREE evening slot
     near 20:00 (never double-booking the partial-unique (responsible_id,start_ts) guard) and set it via
     `db.set_interview_start`, so it lands on his cabinet calendar.
  3. BOOK    — where the recruiter sent a self-schedule link (`mail_index.booking_url`), we CANNOT drive
     those bot-protected JS SPAs from here, so we record «нужна ручная бронь» to a sidecar JSON instead
     of ever fabricating a confirmed booking.

ISOLATION: this module ONLY reads the existing scheduler primitives (pool.py, db.py, hiring_events) and
writes via `db.set_interview_start` + `pool.allocate_specific` + its OWN suppression UPDATE + its own
sidecar/state JSON. It touches no portal route/UI. `--dry-run` changes nothing.

Alan has no Telegram chat_id, so every announce/reminder for his rows would DM the OWNER chat. To avoid
spamming the owner (the Cockpit is Alan's reminder channel), an auto-reserved row is marked
fully-notified (announced + all reminded_* = TRUE) right after scheduling — pass `--notify` to opt back
in to the live Telegram reminders.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.interviews import db, pool
from backend.tools import mail_db

try:
    from backend.tools import hiring_events as _he
except Exception:  # pragma: no cover - hiring_events must import, but never break the lane
    _he = None

RESPONSIBLE_ID = int(os.environ.get("AUTO_RESERVE_RESPONSIBLE_ID", "1035"))  # Alan Bai
DEFAULT_TZ = "Europe/Berlin"
# evening window around 20:00 ±1.5h, 30-min grid, ordered by proximity to 20:00 below.
WINDOW_START_MIN = 18 * 60 + 30      # 18:30
WINDOW_END_MIN = 21 * 60 + 30        # 21:30
SLOT_STEP_MIN = 30
TARGET_MIN = 20 * 60                 # 20:00
ASSIGN_PER_DAY = int(os.environ.get("AUTO_RESERVE_ASSIGN_PER_DAY", "1"))
_STATE = os.path.join(os.path.dirname(__file__), "..", "data", "auto_reserve_state.json")
_MANUAL = os.path.join(os.path.dirname(__file__), "..", "data", "auto_reserve_manual.json")


# --------------------------------------------------------------------------- helpers (pure-ish)
def _resp_tz(rid: int) -> ZoneInfo:
    try:
        r = db.get_responsible(rid) or {}
        return ZoneInfo(r.get("tz") or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _slot_order() -> list[int]:
    """Minutes-of-day for the evening grid, ordered by closeness to 20:00 (20:00, 19:30, 20:30, …)."""
    mins = list(range(WINDOW_START_MIN, WINDOW_END_MIN + 1, SLOT_STEP_MIN))
    return sorted(mins, key=lambda m: (abs(m - TARGET_MIN), m))


def candidate_slots(tz: ZoneInfo, *, days: int = 14, start_offset: int = 1,
                    now: datetime | None = None) -> list[datetime]:
    """Upcoming free evening slots as tz-aware UTC datetimes: for each of the next `days` days
    (starting `start_offset` days from today, local), the grid ordered by proximity to 20:00.
    Day-major, slot-minor — so собесы spread across evenings, each near 20:00."""
    now = now or datetime.now(timezone.utc)
    local_today = now.astimezone(tz).date()
    order = _slot_order()
    out: list[datetime] = []
    for d in range(start_offset, start_offset + days):
        day = local_today + timedelta(days=d)
        for m in order:
            local_dt = datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=tz)
            out.append(local_dt.astimezone(timezone.utc))
    return out


def _taken_starts(rid: int) -> set[datetime]:
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("SELECT start_ts FROM iv_interviews WHERE responsible_id=%s "
                    "AND status<>'cancelled' AND start_ts IS NOT NULL", (rid,))
        return {r[0] for r in cur.fetchall() if r[0] is not None}


def _unscheduled(rid: int) -> list[dict]:
    """Alan's non-cancelled собесы with NO start_ts (need a time), oldest invite first."""
    with mail_db._cur() as cur:
        cur.execute("SELECT id, mailbox, company, jobid FROM iv_interviews "
                    "WHERE responsible_id=%s AND status<>'cancelled' AND start_ts IS NULL "
                    "ORDER BY created_at ASC, id ASC", (rid,))
        return [dict(r) for r in cur.fetchall()]


def _booking_url(mailbox: str) -> tuple[str | None, str | None]:
    try:
        with mail_db._cur() as cur:
            cur.execute("SELECT booking_url, booking_provider FROM mail_index "
                        "WHERE mailbox=%s AND kind='interview' AND booking_url IS NOT NULL "
                        "ORDER BY date_ts DESC NULLS LAST LIMIT 1", (mailbox,))
            r = cur.fetchone()
            return (r["booking_url"], r["booking_provider"]) if r else (None, None)
    except Exception:
        return (None, None)


def _has_resume(mailbox: str) -> bool:
    if _he is None:
        return False
    try:
        return bool(_he.resume_pdf_path(mailbox))
    except Exception:
        return False


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _suppress_notifications(iid: int) -> None:
    """Mark a row fully-notified so the ivremind daemon never DMs the owner for Alan's (TG-less)
    auto-reserved собес. The Mac Cockpit is Alan's reminder channel."""
    with mail_db._cur(dict_rows=False) as cur:
        cur.execute("UPDATE iv_interviews SET announced=TRUE, reminded_120=TRUE, reminded_60=TRUE, "
                    "reminded_15=TRUE, reminded_5=TRUE WHERE id=%s", (iid,))


def _pick_fresh_pool_mailbox() -> str | None:
    """A pool interview to hand Alan — prefer one whose résumé is still on disk (prep can load it)."""
    try:
        rows = pool.unallocated(limit=300, include_expired=False)
    except Exception:
        return None
    if not rows:
        return None
    with_resume = [r["mailbox"] for r in rows if _has_resume(r["mailbox"])]
    return (with_resume or [r["mailbox"] for r in rows])[0] if (with_resume or rows) else None


# --------------------------------------------------------------------------- the lane
def run(dry_run: bool = True, notify: bool = False, now: datetime | None = None) -> dict:
    """One tick. Returns a plan/result summary; never raises into the caller."""
    now = now or datetime.now(timezone.utc)
    rid = RESPONSIBLE_ID
    tz = _resp_tz(rid)
    today = now.astimezone(tz).date().isoformat()
    state = _load_json(_STATE)
    result = {"dry_run": dry_run, "responsible_id": rid, "tz": str(tz), "date": today,
              "assigned": None, "reserved": [], "manual_booking": [], "skipped_assign": None}

    # 1) ASSIGN — at most ASSIGN_PER_DAY per day; only when his unscheduled queue is at/below it
    already = state.get("assigned_on") == today
    unsched = _unscheduled(rid)
    if already:
        result["skipped_assign"] = "already assigned today"
    elif len(unsched) >= max(ASSIGN_PER_DAY * 3, 3):
        result["skipped_assign"] = f"queue not low ({len(unsched)} unscheduled)"
    else:
        mb = _pick_fresh_pool_mailbox()
        if mb:
            result["assigned"] = {"mailbox": mb, "has_resume": _has_resume(mb)}
            if not dry_run:
                try:
                    if pool.allocate_specific(mb, rid):
                        state["assigned_on"] = today
                        _save_json(_STATE, state)
                except Exception as exc:
                    result["assigned"] = {"mailbox": mb, "error": str(exc)}
        else:
            result["skipped_assign"] = "no pool interview available"

    # 2) RESERVE — give every unscheduled собес the next free evening slot near 20:00
    unsched = _unscheduled(rid)                       # re-read (a just-assigned row has NULL start)
    taken = _taken_starts(rid)
    slots = candidate_slots(tz, now=now)
    slot_iter = iter(s for s in slots if s not in taken)
    manual = _load_json(_MANUAL)
    for iv in unsched:
        slot = next(slot_iter, None)
        if slot is None:
            break
        taken.add(slot)
        burl, bprov = _booking_url(iv["mailbox"])
        entry = {"id": iv["id"], "mailbox": iv["mailbox"], "company": iv.get("company"),
                 "start": slot.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"),
                 "booking_url": burl, "needs_manual_booking": bool(burl)}
        result["reserved"].append(entry)
        if burl:
            result["manual_booking"].append({"id": iv["id"], "mailbox": iv["mailbox"],
                                             "booking_url": burl, "provider": bprov})
        if not dry_run:
            try:
                db.set_interview_start(iv["id"], slot, slot + timedelta(hours=1))
                if not notify:
                    _suppress_notifications(iv["id"])
                if burl:
                    manual[str(iv["id"])] = {"mailbox": iv["mailbox"], "booking_url": burl,
                                             "provider": bprov, "start": entry["start"],
                                             "flagged_on": today}
            except Exception as exc:                  # a double-book or transient — skip this row
                entry["error"] = str(exc)
    if not dry_run and manual:
        _save_json(_MANUAL, manual)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily auto-assign + evening-slot reservation for Alan Bai")
    ap.add_argument("--dry-run", action="store_true", help="plan only, change nothing")
    ap.add_argument("--notify", action="store_true",
                    help="leave the ivremind reminders armed (default: suppress, Cockpit reminds)")
    args = ap.parse_args()
    res = run(dry_run=args.dry_run, notify=args.notify)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
