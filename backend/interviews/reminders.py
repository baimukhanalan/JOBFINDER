"""Standalone notifier daemon for the interview scheduler.

Notifier ONLY — no interactive bot, no slot-editing, no aiogram. On a ~60s loop it
sends Telegram messages for `iv_interviews`:
  * a one-time "interview assigned" notification when it first sees an assigned,
    not-yet-announced interview;
  * reminders at -60 min and -5 min before `start_ts`.

Not wired into the dashboard — the controller deploys this as a separate process:
    python -m backend.interviews.reminders
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from backend.interviews import db, notify

logger = logging.getLogger(__name__)


def plan(announcements: list, due60: list, due5: list) -> list[tuple[dict, str]]:
    """PURE planner: flatten/label the three input lists into (interview, kind) pairs,
    kind ∈ {'assigned','60','5'}. No db, no network — unit-testable."""
    pairs: list[tuple[dict, str]] = []
    for iv in announcements:
        pairs.append((iv, "assigned"))
    for iv in due60:
        pairs.append((iv, "60"))
    for iv in due5:
        pairs.append((iv, "5"))
    return pairs


def _responsible_name(interview: dict) -> str:
    rid = interview.get("responsible_id")
    if rid is None:
        return "—"
    resp = db.get_responsible(rid)
    return (resp or {}).get("name") or "—"


def tick() -> int:
    """One notifier pass. Gather due announcements + reminders, send + mark each.
    Each send+mark is wrapped so one bad row can't stop the tick. Returns the number
    of (interview, kind) pairs attempted."""
    now = datetime.now(timezone.utc)
    announcements = db.due_announcements()
    due60 = db.due_reminders(now, 60)
    due5 = db.due_reminders(now, 5)

    pairs = plan(announcements, due60, due5)
    for iv, kind in pairs:
        try:
            name = _responsible_name(iv)
            if kind == "assigned":
                text = notify.assigned_text(iv, name)
            else:
                text = notify.reminder_text(iv, name, int(kind))
            notify.notify_responsible(iv, text)
            # Mark after the send ATTEMPT (not conditional on success) so a permanently
            # bad personal chat_id doesn't re-fire every tick; the owner fallback makes
            # the send usually succeed anyway.
            if kind == "assigned":
                db.mark_announced(iv["id"])
            else:
                db.mark_reminded(iv["id"], kind)
        except Exception as e:
            logger.warning("tick: failed on interview %s kind=%s: %s",
                           iv.get("id"), kind, e)
    return len(pairs)


def run_forever(interval: int = 60) -> None:
    logger.info("interview notifier daemon: started (interval=%ss)", interval)
    schema_ready = False
    while True:
        try:
            # Attempt schema-ensure inside the loop so a brief DB outage at deploy is
            # tolerated like any per-tick transient — the daemon must never hard-exit.
            if not schema_ready:
                db.ensure_schema()
                schema_ready = True
            tick()
        except Exception as e:
            logger.warning("run_forever: cycle failed: %s", e)
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_forever()


if __name__ == "__main__":
    main()
