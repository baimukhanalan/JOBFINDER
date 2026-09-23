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

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.interviews import db, notify, slots

logger = logging.getLogger(__name__)

# ---- weekly «раскидай интервью на неделю» broadcast --------------------------------
# Fired on FRIDAY and SUNDAY once the local (Asia/Almaty) time is ≥ 19:00, exactly ONCE
# per such day. The 60s tick loop would otherwise re-fire it every minute until midnight,
# so a persisted marker (`logs/iv_weekly_nudge.json`, mirroring notify._read/_write_offset)
# records the last-sent date-key `YYYY-MM-DD:fri|sun`; a daemon restart re-reads it, so the
# dedupe survives a restart too.
_NUDGE_DAYS = {4: "fri", 6: "sun"}   # datetime.weekday(): Mon=0 .. Fri=4 .. Sun=6
_NUDGE_HOUR = 19                     # local hour gate (Asia/Almaty)
_NUDGE_MARKER = Path(__file__).resolve().parents[2] / "logs" / "iv_weekly_nudge.json"


def _nudge_marker_key(now: datetime) -> str | None:
    """The date-key for `now` (an aware UTC datetime) IFF it is a Fri/Sun at ≥19:00 local
    (Asia/Almaty) — else None. The key `YYYY-MM-DD:fri|sun` is unique per broadcast day."""
    local = slots.to_local(now, slots.DEFAULT_TZ)
    tag = _NUDGE_DAYS.get(local.weekday())
    if not tag or local.hour < _NUDGE_HOUR:
        return None
    return f"{local.date().isoformat()}:{tag}"


def _read_nudge_marker() -> str:
    try:
        return str(json.loads(_NUDGE_MARKER.read_text()).get("last") or "")
    except Exception:
        return ""


def _write_nudge_marker(key: str) -> None:
    try:
        _NUDGE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _NUDGE_MARKER.write_text(json.dumps({"last": key}))
    except Exception:
        pass


def send_weekly_nudge(now: datetime) -> int:
    """DM every connected manager/interviewer their «распредели/подготовь свою неделю» nudge.
    Best-effort per recipient (one bad row never stops the rest, never raises). Returns how
    many DMs were sent. Does NOT gate on day/hour — the caller (`_maybe_weekly_nudge`) owns
    the Fri/Sun-19:00 + once-per-day gate."""
    try:
        recipients = db.responsibles_for_nudge()
    except Exception as e:
        logger.warning("send_weekly_nudge: recipient lookup failed: %s", e)
        return 0
    sent = 0
    for resp in recipients:
        try:
            chat = resp.get("telegram_chat_id")
            if not chat:
                continue
            rid = resp.get("id")
            if "manager" in db.roles_of(resp):
                # a manager's queue of собесы to раскидать across his team
                rows = db.interviews_held_by(rid, by="responsible")
            else:
                rows = db.interviews_for_responsible(rid, upcoming_only=True)
            pending = len(rows)
            unscheduled = sum(1 for iv in rows if not iv.get("start_ts"))
            text = notify.weekly_nudge_text(resp, pending, notify.PORTAL_BASE, unscheduled)
            if notify.send_dm(int(chat), text):
                sent += 1
        except Exception as e:
            logger.warning("send_weekly_nudge: failed for responsible %s: %s",
                           resp.get("id"), e)
    return sent


def _maybe_weekly_nudge(now: datetime) -> int:
    """Fire the weekly broadcast if `now` opens a fresh Fri/Sun-19:00 window we haven't
    served yet, then persist the marker so the 60s loop (and a restart) won't re-send.
    The marker is written even on a 0-send pass so a transient empty result can't turn the
    once-a-day nudge into an all-evening retry."""
    key = _nudge_marker_key(now)
    if not key or _read_nudge_marker() == key:
        return 0
    sent = send_weekly_nudge(now)
    _write_nudge_marker(key)
    return sent


# ---- live hiring-event: auto-distribute + join-now reminders -----------------------
# So no OFFICIALLY-attendable Zoom room leaks unjoined: every ~14 min the daemon (1) re-runs
# hiring_events.auto_distribute (assigns fresh live candidates to available interviewers) and
# (2) DMs the assigned interviewer to JOIN when the room's window is imminent/open — deduped
# once per (candidate, assignee, day) so the 60s loop can't spam. Both fully guarded/additive.
_EVENT_PASS_MARKER = Path(__file__).resolve().parents[2] / "logs" / "iv_event_pass.json"
_EVENT_REMIND_MARKER = Path(__file__).resolve().parents[2] / "logs" / "iv_event_remind.json"
_EVENT_PASS_GAP = 840          # seconds — re-distribute/remind at most ~every 14 min
_EVENT_REMIND_LEAD = 45        # minutes before the window opens to start reminding


def _event_remind_read() -> set:
    try:
        return set(json.loads(_EVENT_REMIND_MARKER.read_text()).get("sent") or [])
    except Exception:
        return set()


def _event_remind_write(keys: set) -> None:
    try:
        _EVENT_REMIND_MARKER.parent.mkdir(parents=True, exist_ok=True)
        # keep the file bounded (recent keys only)
        _EVENT_REMIND_MARKER.write_text(json.dumps({"sent": sorted(keys)[-4000:]}))
    except Exception:
        pass


def _event_remind_text(inv: dict, group: dict, persona: str) -> str:
    join = inv.get("join_url") or group.get("join_url") or ""
    win = ""
    dt, tt = (inv.get("date_text") or group.get("date_text") or ""), (inv.get("time_text") or group.get("time_text") or "")
    if dt or tt:
        win = f"\nОкно: {(dt + ' ' + tt).strip()}"
    role = inv.get("role") or group.get("role") or "Remote CSR"
    return (f"🎯 Живое событие найма СЕЙЧАС — зайдите в Zoom-комнату под кандидатом:\n"
            f"{persona} ({inv.get('mailbox') or ''})\nРоль: {role}{win}\n"
            f"Ссылка: {join}\nЗайдите в комнату под этим кандидатом в его окно — оффер дают на месте.")


def send_event_reminders(now: datetime) -> int:
    """DM each assigned interviewer to JOIN when their attendable room's window is imminent/open.
    Only OFFICIALLY-attendable rooms (re-verified live here). Deduped per (mailbox, rid, day).
    Returns the number of DMs sent. Fully guarded."""
    try:
        from backend.tools import hiring_events as he
        groups = he.grouped_events(resolve=True)
        he.verify_events([inv for g in groups for inv in (g.get("invites") or [])])
        attend, _exp = he.partition_groups(groups, now.timestamp())
    except Exception as e:
        logger.warning("send_event_reminders: gather failed: %s", e)
        return 0
    try:
        mbs = list({inv.get("mailbox") for g in attend for inv in (g.get("invites") or []) if inv.get("mailbox")})
        claims = db.event_claims_for(mbs)
    except Exception:
        claims = {}
    seen = _event_remind_read()
    daykey = now.astimezone(timezone.utc).date().isoformat()
    sent = 0
    dirty = False
    for g in attend:
        for inv in (g.get("invites") or []):
            if inv.get("_status") == "expired":
                continue
            mb = inv.get("mailbox")
            cl = claims.get(mb) or []
            if not cl:
                continue                                   # only remind an ASSIGNED/claimed room
            try:
                occ = he.next_occurrence_utc(he.parse_event_window(inv.get("date_text"), inv.get("time_text")), now)
            except Exception:
                occ = None
            if not occ:
                continue
            s_utc, e_utc = occ
            from datetime import timedelta as _td
            if not (s_utc - _td(minutes=_EVENT_REMIND_LEAD) <= now <= e_utc):
                continue                                   # not imminent/open yet (or already past)
            for c in cl:
                rid = c.get("responsible_id")
                key = f"{mb}|{rid}|{daykey}"
                if key in seen:
                    continue
                seen.add(key)
                dirty = True
                try:
                    r = db.get_responsible(rid) or {}
                    chat = r.get("telegram_chat_id")
                    if not chat:
                        continue                           # can't DM (marked seen → no retry storm)
                    persona = (c.get("name") and inv.get("candidate")) or inv.get("candidate") or (inv.get("mailbox") or "")
                    if notify.send_dm(int(chat), _event_remind_text(inv, g, inv.get("candidate") or persona)):
                        sent += 1
                        try:
                            rp = he.resume_pdf_path(mb)
                            if rp:
                                notify.send_document(int(chat), rp, caption="Резюме")
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning("send_event_reminders: rid=%s mbx=%s failed: %s", rid, mb, e)
    if dirty:
        _event_remind_write(seen)
    return sent


def _maybe_event_pass(now: datetime) -> None:
    """Throttled (~14 min) combined pass: auto-distribute fresh live rooms, then remind assigned
    interviewers whose window is open. Guarded — never breaks the tick."""
    try:
        last = float(json.loads(_EVENT_PASS_MARKER.read_text()).get("ts") or 0)
    except Exception:
        last = 0.0
    if now.timestamp() - last < _EVENT_PASS_GAP:
        return
    try:
        _EVENT_PASS_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _EVENT_PASS_MARKER.write_text(json.dumps({"ts": now.timestamp()}))
    except Exception:
        pass
    try:
        from backend.tools import hiring_events as he
        he.auto_distribute(now)
    except Exception as e:
        logger.warning("_maybe_event_pass: distribute failed: %s", e)
    try:
        send_event_reminders(now)
    except Exception as e:
        logger.warning("_maybe_event_pass: reminders failed: %s", e)


def plan(announcements: list, due60: list, due5: list,
         due120: list | None = None, due15: list | None = None) -> list[tuple[dict, str]]:
    """PURE planner: flatten/label the input lists into (interview, kind) pairs,
    kind ∈ {'assigned','120','60','15','5'}. No db, no network — unit-testable.
    due120/due15 are optional (kept last) so existing 3-arg callers/tests are unaffected."""
    pairs: list[tuple[dict, str]] = []
    for iv in announcements:
        pairs.append((iv, "assigned"))
    for iv in (due120 or []):
        pairs.append((iv, "120"))
    for iv in due60:
        pairs.append((iv, "60"))
    for iv in (due15 or []):
        pairs.append((iv, "15"))
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

    # ADDITIVE: the Fri/Sun-19:00 weekly «раскидай интервью на неделю» broadcast. Fully
    # guarded — never affects the reminder/announcement pass below nor the returned count.
    try:
        _maybe_weekly_nudge(now)
    except Exception as e:
        logger.warning("tick: weekly nudge failed: %s", e)

    # ADDITIVE: throttled (~14 min) auto-distribute of live hiring-event rooms to available
    # interviewers + join-now DM reminders. Fully guarded — never affects the reminder pass below.
    try:
        _maybe_event_pass(now)
    except Exception as e:
        logger.warning("tick: event pass failed: %s", e)

    announcements = db.due_announcements()
    due120 = db.due_reminders(now, 120)
    due60 = db.due_reminders(now, 60)
    due15 = db.due_reminders(now, 15)
    due5 = db.due_reminders(now, 5)

    pairs = plan(announcements, due60, due5, due120, due15)
    for iv, kind in pairs:
        try:
            name, tz = _responsible_meta(iv)
            if kind == "assigned":
                text = notify.assigned_text(iv, name, tz)
            elif kind == "120":
                # -2h walk-in prep (prepare / bring your own ID; we never fabricate one)
                text = notify.walkin_prep_text(iv, name, tz)
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
            # Walk-in reminders (-2h prep, -15 min) are ADMIN-facing → the dedicated admin bot
            # (@jobfinderadminnbot). The standard scheduler notices (assigned / -60 rich / -5) stay
            # on the responsible-facing bot (@crmjobfinderbot).
            if kind in ("120", "15"):
                notify.send_admin(text)
            else:
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
