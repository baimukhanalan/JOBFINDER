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


def _responsible_meta(interview: dict) -> tuple[str, str | None]:
    """(name, tz) for the interview's responsible — times are shown in THEIR zone."""
    rid = interview.get("responsible_id")
    if rid is None:
        return "—", None
    resp = db.get_responsible(rid) or {}
    return (resp.get("name") or "—"), resp.get("tz")


def tick() -> int:
    """One notifier pass. Gather due announcements + reminders, send + mark each.
    Each send+mark is wrapped so one bad row can't stop the tick. Returns the number
    of (interview, kind) pairs attempted."""
    try:
        notify.poll_updates()  # process self-service Telegram linking (/start <code>)
    except Exception as e:
        logger.warning("tick: poll_updates failed: %s", e)

    now = datetime.now(timezone.utc)
    announcements = db.due_announcements()
    due60 = db.due_reminders(now, 60)
    due5 = db.due_reminders(now, 5)

    pairs = plan(announcements, due60, due5)
    for iv, kind in pairs:
        try:
            name, tz = _responsible_meta(iv)
            if kind == "assigned":
                text = notify.assigned_text(iv, name, tz)
            elif kind == "60":
                # the -60 reminder is the RICH one: company · role · persona · Zoom link,
                # plus the tailored résumé PDF as an attachment.
                from backend.interviews import service
                pack = service.interview_pack(iv)
                text = notify.rich_reminder_text(iv, name, tz, pack)
                notify.notify_responsible(iv, text)
                if pack.get("resume_path"):
                    chat = notify.target_chat(iv)
                    if chat:
                        notify.send_document(chat, pack["resume_path"],
                                             caption=f"Резюме — {pack.get('persona_name') or ''}")
                db.mark_reminded(iv["id"], kind)
                continue
            else:
                text = notify.reminder_text(iv, name, int(kind), tz)
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
