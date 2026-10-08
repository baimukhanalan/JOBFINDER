"""Interview Cockpit — Telegram CONFIRM + Mac AUTO-WAKE bridge (server-side).

Flow (owner 2026-10-08): ~40 min before a собес assigned to Alan Bai (id 1035), the live
`jobfinder-alan-ivremind` daemon DMs an inline-keyboard «Будешь за маком Алана?». On «✅ Буду»
we (a) record the confirmation and (b) WAKE/keep-awake Alan's Mac for the собес time and STAGE the
candidate (`cockpit_prep`) so the native Cockpit app finds `next.json` and preps Natively + the
browser at the Zoom room. On «❌ Не буду» we just record it.

Split cleanly:
  * PURE helpers (no I/O): confirm keys, callback parsing, the wake-vs-caffeinate decision, the
    pmset/caffeinate timing — all unit-tested.
  * SIDE-EFFECTING: the TG send (via `notify`), the `ssh macalan` wake bridge, the stage trigger.

Wake-from-sleep needs `pmset schedule wake`, which needs sudo. On this Mac sudo needs a password, so:
  * if `sudo -n pmset` works (owner added the sudoers line below) → real wake-from-sleep is scheduled;
  * else → a detached `caffeinate` holds the Mac awake across the собес window (works only if the Mac
    is awake when we trigger — it can't wake a sleeping Mac without pmset).
ONE-TIME owner setup to enable true wake (run once on the Mac):
    echo 'alanbaimukhan ALL=(root) NOPASSWD: /usr/bin/pmset schedule wake *' \
      | sudo tee /etc/sudoers.d/jobfinder-pmset && sudo chmod 440 /etc/sudoers.d/jobfinder-pmset

Everything is guarded + idempotent; the Mac being offline is a soft failure, never an exception.
Touching this (via reminders/notify) needs `pm2 restart jobfinder-alan-ivremind` to go live.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ALAN_ID = 1035
MAC_SSH = os.environ.get("COCKPIT_MAC_SSH", "macalan")

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_CONFIRMS = os.path.join(_DATA, "cockpit_confirms.json")
_ASK_SENT = os.path.join(_DATA, "cockpit_ask_sent.json")

ASK_LEAD_MIN = int(os.environ.get("COCKPIT_ASK_LEAD_MIN", "45"))   # start asking this long before
ASK_FLOOR_MIN = int(os.environ.get("COCKPIT_ASK_FLOOR_MIN", "8"))  # stop asking once this close
WAKE_LEAD_MIN = int(os.environ.get("COCKPIT_WAKE_LEAD_MIN", "6"))  # wake this many min before start
SESSION_MIN = int(os.environ.get("COCKPIT_SESSION_MIN", "75"))     # assumed собес duration
_CB_PREFIX = "ck"                                                  # callback_data namespace


# ---------------------------------------------------------------------------- PURE helpers (tested)
def confirm_key(iv: dict) -> str:
    """A stable per-собес key: interview id + its start date (so a re-timed row re-asks)."""
    iid = iv.get("id")
    start = iv.get("start_ts")
    day = ""
    try:
        if start is not None:
            s = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            day = s.astimezone(timezone.utc).date().isoformat()
    except Exception:
        day = ""
    return f"{iid}:{day}"


def callback_data(action: str, iv: dict) -> str:
    """Build the inline-button callback_data (≤64 bytes): `ck:y:<iid>` / `ck:n:<iid>`."""
    a = "y" if action == "yes" else "n"
    return f"{_CB_PREFIX}:{a}:{iv.get('id')}"


def parse_callback_data(data: str) -> tuple[str, int] | None:
    """`ck:y:123` → ('yes', 123); `ck:n:123` → ('no', 123); anything else → None."""
    if not data or not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != _CB_PREFIX or parts[1] not in ("y", "n"):
        return None
    try:
        return ("yes" if parts[1] == "y" else "no"), int(parts[2])
    except (ValueError, TypeError):
        return None


def wake_decision(sudo_pmset_ok: bool) -> str:
    """'pmset' (true wake-from-sleep) when the sudoers line is in place, else 'caffeinate' (hold awake)."""
    return "pmset" if sudo_pmset_ok else "caffeinate"


def _delta_secs(now: datetime, start_ts: datetime) -> float:
    s = start_ts if getattr(start_ts, "tzinfo", None) else start_ts.replace(tzinfo=timezone.utc)
    n = now if getattr(now, "tzinfo", None) else now.replace(tzinfo=timezone.utc)
    return (s - n).total_seconds()


def pmset_wake_delta_secs(now: datetime, start_ts: datetime, lead_min: int = WAKE_LEAD_MIN) -> int:
    """Seconds from `now` until (start − lead). Floored at 60s so the wake is always in the future
    (the Mac computes the absolute local time via `date -v+<N>S`, so this stays tz-agnostic)."""
    return max(60, int(_delta_secs(now, start_ts) - lead_min * 60))


def caffeinate_secs(now: datetime, start_ts: datetime, lead_min: int = WAKE_LEAD_MIN,
                    session_min: int = SESSION_MIN, cap_secs: int = 4 * 3600) -> int:
    """Seconds to hold the Mac awake: from now through the собес window (start−lead … start+session),
    floored at 300s, capped so a far-future / bad start can't pin it awake for hours."""
    secs = int(_delta_secs(now, start_ts) - lead_min * 60) + (lead_min + session_min) * 60
    return max(300, min(secs, cap_secs))


def should_ask(now: datetime, start_ts, *, lead_min: int = ASK_LEAD_MIN,
               floor_min: int = ASK_FLOOR_MIN) -> bool:
    """True when `now` is inside the ask window [start−lead … start−floor] (so we ask ~40 min out,
    not at the last second and not for far-future/past собесы). start_ts None → False."""
    if not start_ts:
        return False
    d = _delta_secs(now, start_ts)
    return floor_min * 60 <= d <= lead_min * 60


# ------------------------------------------------------------------------------- sidecar state (I/O)
def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("cockpit_wake: save %s failed: %s", path, e)


def ask_already_sent(key: str) -> bool:
    return key in (_load(_ASK_SENT).get("sent") or [])


def mark_ask_sent(key: str) -> None:
    d = _load(_ASK_SENT)
    sent = set(d.get("sent") or [])
    sent.add(key)
    _save(_ASK_SENT, {"sent": sorted(sent)[-4000:]})


def record_confirm(key: str, answer: str) -> None:
    d = _load(_CONFIRMS)
    d[key] = {"answer": answer, "ts": datetime.now(timezone.utc).isoformat()}
    _save(_CONFIRMS, d)


def confirmed_yes(key: str) -> bool:
    return (_load(_CONFIRMS).get(key) or {}).get("answer") == "yes"


# --------------------------------------------------------------------------------- Mac wake bridge
def _ssh(args: list[str], timeout: int = 40) -> tuple[bool, str]:
    try:
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", MAC_SSH] + args,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()[-300:]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _mac_online() -> bool:
    ok, _ = _ssh(["true"], timeout=25)
    return ok


def _sudo_pmset_ok() -> bool:
    """Does `sudo -n pmset` run without a password (owner added the sudoers line)?"""
    ok, _ = _ssh(["sudo", "-n", "pmset", "-g", "sched"], timeout=25)
    return ok


def wake_mac(now: datetime, start_ts: datetime) -> dict:
    """Schedule a real wake-from-sleep (pmset, if sudo-enabled) else hold the Mac awake across the
    собес window (caffeinate). Returns {mechanism, ok, mac_online, detail}. Never raises."""
    out = {"mechanism": None, "ok": False, "mac_online": False, "detail": ""}
    if not _mac_online():
        out["detail"] = "mac offline"
        return out
    out["mac_online"] = True
    if _sudo_pmset_ok():
        out["mechanism"] = "pmset"
        delta = pmset_wake_delta_secs(now, start_ts)
        # compute the absolute LOCAL wake time ON the Mac (tz-agnostic) then schedule it
        cmd = (f'T=$(date -v+{delta}S "+%m/%d/%y %H:%M:%S"); '
               f'sudo -n pmset schedule wake "$T" && echo "waked $T"')
        out["ok"], out["detail"] = _ssh(["sh", "-lc", cmd], timeout=40)
    else:
        out["mechanism"] = "caffeinate"
        secs = caffeinate_secs(now, start_ts)
        cmd = (f'nohup caffeinate -dimsu -t {secs} >/dev/null 2>&1 & disown; '
               f'echo "caffeinate {secs}s"')
        out["ok"], out["detail"] = _ssh(["sh", "-lc", cmd], timeout=30)
    return out


def _stage(iv: dict) -> dict:
    """Stage the собес onto the Mac inbox so the native app preps it. Guarded; returns the result."""
    try:
        from backend.interviews import cockpit_prep
        manifest, pack = cockpit_prep.build_manifest(iv["mailbox"], iv_row=iv)
        return cockpit_prep.stage_to_mac(manifest, pack.resume_path)
    except Exception as e:
        logger.warning("cockpit_wake: stage failed for %s: %s", iv.get("mailbox"), e)
        return {"error": str(e)}


def wake_and_prep(iv: dict, now: datetime | None = None) -> dict:
    """On a confirmation: wake/keep-awake the Mac for the собес time + stage the candidate. Guarded."""
    now = now or datetime.now(timezone.utc)
    result = {"wake": None, "stage": None}
    start = iv.get("start_ts")
    if start is not None:
        result["wake"] = wake_mac(now, start)
    result["stage"] = _stage(iv)
    logger.info("cockpit_wake: wake_and_prep iid=%s wake=%s stage=%s",
                iv.get("id"), result["wake"], result["stage"])
    return result


# ------------------------------------------------------------------------------ TG confirm senders
def _ask_chat() -> int | None:
    """Where the «будешь за маком?» question goes: Alan's personal chat if linked, else the owner
    chat (settings.telegram_chat_id). Must be a chat the notifier bot (_bot_token) can poll back."""
    from backend.config import settings
    from backend.interviews import db
    try:
        alan = db.get_responsible(ALAN_ID) or {}
    except Exception:
        alan = {}
    chat = alan.get("telegram_chat_id") or settings.telegram_chat_id
    try:
        return int(chat) if chat else None
    except (ValueError, TypeError):
        return None


def ask_confirm(iv: dict) -> bool:
    """DM the «✅ Буду за маком Алана / ❌ Не буду» inline-keyboard for one собес. Returns whether sent.
    Deduped by the caller via mark_ask_sent."""
    from backend.interviews import notify
    chat = _ask_chat()
    if not chat:
        return False
    who = (iv.get("mailbox") or "").split("@", 1)[0] or "кандидат"
    company = iv.get("company") or "—"
    when = ""
    try:
        start = iv.get("start_ts")
        if start is not None:
            s = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            from backend.interviews import slots
            when = slots.to_local(s, slots.DEFAULT_TZ).strftime("%d.%m %H:%M")
    except Exception:
        when = ""
    text = (f"🖥 Собеседование скоро — будешь проводить его за маком Алана?\n"
            f"Кандидат: {who}\nКомпания: {company}" + (f"\nВремя: {when}" if when else "") +
            "\n\nЕсли «Буду» — подготовлю Natively с резюме и разбужу мак автоматически.")
    buttons = [[{"text": "✅ Буду за маком Алана", "callback_data": callback_data("yes", iv)},
                {"text": "❌ Не буду", "callback_data": callback_data("no", iv)}]]
    return notify.send_dm_buttons(chat, text, buttons)


def handle_callback(action: str, iid: int, *, chat_id: int | None = None,
                    callback_query_id: str | None = None) -> dict:
    """Process a pressed «Буду/Не буду» button: record the answer and, on «yes», wake+prep the Mac.
    Guarded; returns a small summary. Called from notify.poll_updates for `ck:*` callbacks."""
    from backend.interviews import db, notify
    out = {"action": action, "iid": iid, "wake_prep": None, "notice": ""}
    try:
        iv = db.interview_by_id(iid)
    except Exception:
        iv = None
    key = confirm_key(iv) if iv else f"{iid}:"
    record_confirm(key, action)
    if action == "yes":
        out["notice"] = "Готовлю мак и Natively к собесу ✅"
        if iv:
            out["wake_prep"] = wake_and_prep(iv)
    else:
        out["notice"] = "Понял, за маком Алана не проводишь ❌"
    # best-effort UX: pop the button toast + a short message
    try:
        if callback_query_id:
            notify.answer_callback(callback_query_id, out["notice"])
        if chat_id:
            notify.send_dm(int(chat_id), out["notice"])
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- the per-tick cockpit pass
def _due_to_ask(now: datetime) -> list[dict]:
    """Alan's non-cancelled собесы whose start is inside the ask window and not yet asked (not test)."""
    from backend.tools import mail_db
    rows: list[dict] = []
    try:
        with mail_db._cur() as cur:
            cur.execute(
                """SELECT id, mailbox, company, jobid, start_ts FROM iv_interviews
                   WHERE responsible_id=%s AND status<>'cancelled' AND start_ts IS NOT NULL
                     AND mailbox NOT LIKE 'test_iv_%%'
                     AND start_ts BETWEEN now() AND now() + interval '%s minutes'
                   ORDER BY start_ts""",
                (ALAN_ID, ASK_LEAD_MIN))
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("cockpit_wake: _due_to_ask failed: %s", e)
    return [iv for iv in rows if should_ask(now, iv.get("start_ts"))]


def maybe_cockpit_pass(now: datetime | None = None) -> int:
    """Tick hook (guarded, additive): ask the confirm for any собес entering the ask window, once.
    Returns how many asks were sent. Does NOT itself wake — the owner's button press triggers that."""
    now = now or datetime.now(timezone.utc)
    sent = 0
    for iv in _due_to_ask(now):
        key = confirm_key(iv)
        if ask_already_sent(key):
            continue
        try:
            if ask_confirm(iv):
                sent += 1
        except Exception as e:
            logger.warning("cockpit_wake: ask_confirm failed iid=%s: %s", iv.get("id"), e)
        mark_ask_sent(key)   # mark even on failure so a bad chat can't re-ask every tick
    return sent
