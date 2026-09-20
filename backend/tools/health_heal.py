"""Self-healing health watcher — turns `health.gather()` from *detect + alert* into *detect, AUTO-FIX
the safe failure modes, and alert only on what it cannot fix*.

The owner wants known failures repaired the moment they arise (not merely noticed). This runs
`health.gather()`, classifies every RED/actionable row against a table of KNOWN, BOUNDED, IDEMPOTENT
remediations, applies them, and Telegram-alerts (reusing `health`'s notifier) on anything unknown or
un-fixable.

Remediation table (mode → action → the process it reuses):
  * pm2_restart  a jobfinder-* service `down` (errored/stopped/hung/crash-looping)  → `pm2 restart <name>`
  * llm_restart  the local model (127.0.0.1:8080) unreachable ("не отвечает")        → `pm2 restart llm-server`
  * egress_sync  a dead exit-node egress slot (demon died)                           → `tailscale_egress --sync`
  * mac_tunnel   Mac/Sutherland lane running but the Mac CDP is dead                 → `tailscale_egress --sync`
  * chrome_reap  too many JobFinder Chromium (leaked/stuck browsers)                 → `chrome_reaper --min-age`
  * alert        anything else `down`, OR a KNOWN mode that a restart can't fix      → Telegram (throttled)
    (e.g. pm2 cwd-mismatch / process-removed, or the LLM reachable-but-500 = expired owner token)

HARD SAFETY:
  * Only the bounded, idempotent, well-understood actions above — NEVER a free-form edit, NEVER a
    destructive data action, NEVER `pm2 save`/recreate.
  * PER-TARGET COOLDOWN (`HEAL_COOLDOWN_SECS`, default 300s) so consecutive ticks can't hammer one target.
  * CIRCUIT BREAKER (`HEAL_BREAKER_MAX` heals within `HEAL_BREAKER_WINDOW_SECS`, default 3 / 3600s):
    once a target has been healed that many times in the window and is STILL failing, auto-repair STOPS
    for it and the owner is alerted — no restart-storm.
  * Re-entrant: an fcntl lock (`logs/health_heal.lock`) makes overlapping cron ticks a no-op.
  * `--dry-run` reports exactly what it WOULD heal (and each target's cooldown/breaker decision) and
    changes nothing — no subprocess, no state write, no Telegram.

Cron (user `programmer`; no `sg mail`/`DISPLAY` needed — each remediation subprocess handles its own):
    */10 * * * * cd /home/projects/jobfinder && env PYTHONPATH=. python3 -m backend.tools.health_heal >> logs/health_heal.log 2>&1
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

from backend.tools import health

_ROOT = health._ROOT
_LOGS = health._LOGS
_STATE = os.path.join(_LOGS, "health_heal_state.json")
_LOCK = os.path.join(_LOGS, "health_heal.lock")
_TS_AUTHKEY = os.path.join(_ROOT, "backend", ".ts_authkey")


def _envint(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except Exception:
        return default


# tunables (env-overridable; defaults are conservative)
HEAL_COOLDOWN = _envint("HEAL_COOLDOWN_SECS", 300)          # min gap between heals of ONE target
BREAKER_MAX = _envint("HEAL_BREAKER_MAX", 3)               # heals per target per window before tripping
BREAKER_WINDOW = _envint("HEAL_BREAKER_WINDOW_SECS", 3600)  # rolling breaker window
ALERT_COOLDOWN = _envint("HEAL_ALERT_COOLDOWN_SECS", 14400)  # 4h — matches health --alert
CHROME_MIN_AGE = _envint("HEAL_CHROME_MIN_AGE", 3600)      # only reap orphan chromium older than this
_CMD_TIMEOUT = _envint("HEAL_CMD_TIMEOUT_SECS", 150)        # hard deadline per remediation subprocess


# --------------------------------------------------------------------------------------------------
# CLASSIFIER — PURE: snapshot rows → remediation plan. No I/O, no gather(), no subprocess.
# Each action: {kind: 'heal'|'alert', target, mode, action, command:[...]|None, name, detail, note}
# --------------------------------------------------------------------------------------------------
def _egress_cmd() -> list[str]:
    """`tailscale_egress --sync`, adding the reusable authkey file when present (else a plain reap-safe
    sync). Kept in one place so both the dead-slot and the Mac-tunnel modes agree byte-for-byte (so the
    executor de-dupes them to a single run per tick)."""
    cmd = [sys.executable, "-m", "backend.tools.tailscale_egress", "--sync"]
    if os.path.exists(_TS_AUTHKEY):
        cmd += ["--authkey", "file:backend/.ts_authkey"]
    return cmd


def _heal(target, mode, command, name, detail, action, note=""):
    return {"kind": "heal", "target": target, "mode": mode, "command": command,
            "name": name, "detail": detail, "action": action, "note": note}


def _alert(target, mode, name, detail, note):
    return {"kind": "alert", "target": target, "mode": mode, "command": None,
            "name": name, "detail": detail, "action": "Telegram-алерт", "note": note}


def classify_row(section_key: str, row: dict):
    """One health row → an action, or None (nothing to do). PURE."""
    name = str(row.get("name", ""))
    status = str(row.get("status", ""))
    detail = str(row.get("detail", ""))
    low = detail.lower()

    # --- pm2 services (gather()'s pm2 section only carries jobfinder-* rows + a generic failure row) ---
    if section_key == "pm2":
        if name.startswith("jobfinder-"):
            if status != "down":
                return None
            # A wrong cwd or a removed process is NOT fixable by `restart` — it needs `pm2 start --cwd`
            # (see Deploy). Alert instead of a useless restart that would burn the breaker.
            if ("cwd" in low and "≠" in detail) or "не найден" in low:
                return _alert(f"pm2:{name}", "pm2_manual", name, detail,
                              "restart не поможет — нужен pm2 start --cwd /home/projects/jobfinder + pm2 save")
            return _heal(f"pm2:{name}", "pm2_restart", ["pm2", "restart", name], name, detail,
                         f"pm2 restart {name}")
        # a non-jobfinder pm2 row that is down = the pm2 layer itself → alert, never auto-touch pm2
        if status == "down":
            return _alert("pm2:layer", "alert", name, detail, "сбой слоя pm2 — не авто-чиним pm2 сам")
        return None

    # --- local model (Sumrak on 127.0.0.1:8080), a pm2 process named llm-server ---
    if section_key == "deps" and name.startswith("Локальная модель"):
        if status != "down":
            return None
        # REACHABLE but erroring (HTTP 4xx/5xx, e.g. "Codex refresh token expired" → 500) — a restart
        # does NOT fix an expired owner token, and looping it is exactly the storm we must avoid. Alert.
        if "http" in low:
            return _alert("llm:token", "llm_token", name, detail,
                          "модель отвечает ошибкой (вероятно истёк токен) — авто-рестарт НЕ применяем, нужен owner")
        # UNREACHABLE (connection refused / hung / timeout) → the process is down or wedged → restart it.
        return _heal("llm:restart", "llm_restart", ["pm2", "restart", "llm-server"], name, detail,
                     "pm2 restart llm-server")

    # --- dead exit-node egress slot (demon died; slot advertised but not alive) ---
    if section_key == "deps" and name.startswith("Exit-node мост"):
        if status in ("warn", "down") and ("демон" in low or "отвал" in low):
            return _heal("egress:sync", "egress_sync", _egress_cmd(), name, detail,
                         "tailscale_egress --sync (пересобрать мёртвый слот)")
        return None

    # --- Mac/Sutherland CDP tunnel dead while a drive is running ---
    if section_key == "assessments" and name.startswith("Лэйн Mac/Sutherland"):
        if status == "down":
            return _heal("mac:tunnel", "mac_tunnel", _egress_cmd(), name, detail,
                         "tailscale_egress --sync (пересобрать egress-слот под CDP-туннель)")
        return None

    # --- too many JobFinder Chromium (leaked/stuck browsers past the reaper age) ---
    if section_key == "system" and name.startswith("Chromium"):
        if status in ("warn", "down"):
            return _heal("chrome:reap", "chrome_reap",
                         [sys.executable, "-m", "backend.tools.chrome_reaper", "--min-age", str(CHROME_MIN_AGE)],
                         name, detail, f"chrome_reaper --min-age {CHROME_MIN_AGE} (убить осиротевшие браузеры)")
        return None

    return None


# rows a known mode already OWNS — so the generic "unknown down" alert doesn't double-count them
_KNOWN_DOWN_PREFIXES = ("Локальная модель", "Exit-node мост", "Лэйн Mac/Sutherland", "Chromium")


def classify(snapshot: dict) -> list[dict]:
    """Full snapshot → ordered action list (heals first, then alerts). PURE (operates on the dict)."""
    heals, alerts, handled = [], [], set()
    for sec in snapshot.get("sections", []):
        gk = sec.get("key", "")
        for row in sec.get("rows", []):
            act = classify_row(gk, row)
            if act is None:
                continue
            handled.add((gk, row.get("name")))
            (heals if act["kind"] == "heal" else alerts).append(act)
    # every remaining RED row with no known remedy → a plain alert (reuse the same throttle)
    for sec in snapshot.get("sections", []):
        gk = sec.get("key", "")
        if gk == "incidents":                       # static info rows, never actionable
            continue
        for row in sec.get("rows", []):
            if row.get("status") != "down":
                continue
            name = str(row.get("name", ""))
            if (gk, row.get("name")) in handled:
                continue
            if gk == "pm2" and name.startswith("jobfinder-"):
                continue                            # already a pm2_restart / pm2_manual action
            if gk == "deps" and name.startswith(_KNOWN_DOWN_PREFIXES):
                continue
            alerts.append(_alert(f"down:{gk}:{name}", "alert", name, str(row.get("detail", "")),
                                 "нет известного авто-лечения — требуется владелец"))
    return heals + alerts


# --------------------------------------------------------------------------------------------------
# COOLDOWN + CIRCUIT BREAKER — PURE. Operates on a plain dict state; no file I/O in these methods.
# --------------------------------------------------------------------------------------------------
class HealLedger:
    def __init__(self, state: dict | None = None):
        self.state = state if isinstance(state, dict) else {}
        self.state.setdefault("targets", {})
        self.state.setdefault("alerts", {})

    def _rec(self, target: str) -> dict:
        return self.state["targets"].setdefault(target, {"attempts": []})

    def _pruned(self, target: str, now: float, window: int) -> list[int]:
        rec = self._rec(target)
        rec["attempts"] = [int(t) for t in rec["attempts"] if now - float(t) <= window]
        return rec["attempts"]

    def recent_count(self, target: str, now: float, window: int = BREAKER_WINDOW) -> int:
        return len(self._pruned(target, now, window))

    def last_attempt(self, target: str, now: float, window: int = BREAKER_WINDOW) -> int:
        att = self._pruned(target, now, window)
        return max(att) if att else 0

    def decision(self, target: str, now: float, *, cooldown: int = HEAL_COOLDOWN,
                 max_heals: int = BREAKER_MAX, window: int = BREAKER_WINDOW) -> tuple[str, str]:
        """('heal'|'cooldown'|'breaker', human reason). The breaker is checked FIRST: once it has
        healed `max_heals` times in the window it stays OPEN until the oldest attempt ages out."""
        cnt = self.recent_count(target, now, window)
        if cnt >= max_heals:
            return "breaker", f"{cnt} починок за {window // 60} мин — предохранитель разомкнут, авто-лечение остановлено"
        last = self.last_attempt(target, now, window)
        if last and now - last < cooldown:
            return "cooldown", f"{int(now - last)}с с прошлой починки < {cooldown}с"
        return "heal", "готов к починке"

    def record(self, target: str, now: float) -> None:
        self._rec(target)["attempts"].append(int(now))

    # --- alert throttle (dedupe identical unresolved states within a cooldown) ---
    def should_alert(self, key: str, signature: str, now: float, cooldown: int = ALERT_COOLDOWN) -> bool:
        a = self.state["alerts"].get(key, {})
        return not (a.get("sig") == signature and now - float(a.get("ts", 0)) < cooldown)

    def mark_alert(self, key: str, signature: str, now: float) -> None:
        self.state["alerts"][key] = {"sig": signature, "ts": int(now)}


# --------------------------------------------------------------------------------------------------
# I/O + executor
# --------------------------------------------------------------------------------------------------
def _load_state() -> dict:
    return health._load_json(_STATE)


def _save_state(state: dict) -> None:
    health._save_json(_STATE, state)


def _run_cmd(command: list[str]) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": _ROOT}
    try:
        p = subprocess.run(command, cwd=_ROOT, env=env, capture_output=True, text=True,
                           timeout=_CMD_TIMEOUT)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()[-400:]
    except subprocess.TimeoutExpired:
        return 124, f"timeout >{_CMD_TIMEOUT}s"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def _acquire_lock():
    """fcntl re-entrancy lock. Returns the held fd, or None if another tick owns it."""
    try:
        os.makedirs(_LOGS, exist_ok=True)
        fd = open(_LOCK, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        return None


def heal(dry_run: bool = False, snapshot: dict | None = None) -> dict:
    """Run one heal tick. Never raises. Returns a summary dict; logs every action to stdout."""
    now = time.time()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    snap = snapshot if snapshot is not None else health.gather()
    actions = classify(snap)
    ledger = HealLedger(_load_state())

    fixed: list[dict] = []          # heals applied this tick
    unresolved: list[dict] = []     # alert-kind + breaker escalations
    skipped: list[str] = []
    log: list[str] = []

    # 1) evaluate every heal action against cooldown + breaker
    runnable_by_cmd: dict[tuple, list[dict]] = {}
    for act in [a for a in actions if a["kind"] == "heal"]:
        decision, why = ledger.decision(act["target"], now)
        act["decision"], act["why"] = decision, why
        if decision == "heal":
            runnable_by_cmd.setdefault(tuple(act["command"]), []).append(act)
        elif decision == "cooldown":
            skipped.append(f"{act['target']} (cooldown: {why})")
            log.append(f"{ts} SKIP {act['target']}: {why}")
        else:  # breaker OPEN → stop auto-repair for this target, escalate
            unresolved.append({**act, "kind": "alert", "note": why})
            log.append(f"{ts} BREAKER {act['target']}: {why}")

    # 2) run each distinct command once (a command shared by two targets — e.g. egress sync — runs once)
    for command, group in runnable_by_cmd.items():
        targets = ", ".join(a["target"] for a in group)
        if dry_run:
            log.append(f"{ts} WOULD heal [{targets}]: {group[0]['action']} :: {' '.join(command)}")
            for a in group:
                fixed.append({**a, "rc": None, "ok": None})
            continue
        rc, out = _run_cmd(list(command))
        ok = rc == 0
        for a in group:
            ledger.record(a["target"], now)
            fixed.append({**a, "rc": rc, "ok": ok})
        log.append(f"{ts} HEAL [{targets}] rc={rc} ok={ok}: {group[0]['action']}"
                   + (f" :: {out[:160]}" if out else ""))

    # 3) plain alerts (unknown down / non-remediable known modes)
    for act in [a for a in actions if a["kind"] == "alert"]:
        unresolved.append(act)
        log.append(f"{ts} ALERT {act['target']}: {act['name']} — {act['note']}")

    # 4) notify (throttled by signature; never in dry-run)
    alerted = False
    if not dry_run and (fixed or unresolved):
        msg = _build_message(ts, fixed, unresolved)
        sig = _signature(fixed, unresolved)
        if ledger.should_alert("heal", sig, now):
            if health._tg(msg):
                ledger.mark_alert("heal", sig, now)
                alerted = True
                log.append(f"{ts} Telegram sent")
        else:
            log.append(f"{ts} Telegram throttled (unchanged state)")

    if not dry_run:
        _save_state(ledger.state)

    for line in log:
        print(line, flush=True)
    if not log:
        print(f"{ts} health_heal: nothing to do (all clear)", flush=True)

    return {"ts": ts, "dry_run": dry_run, "overall": snap.get("overall"),
            "fixed": [f"{f['target']}({'?' if f['ok'] is None else 'ok' if f['ok'] else 'FAIL'})" for f in fixed],
            "unresolved": [u["target"] for u in unresolved],
            "skipped": skipped, "alerted": alerted}


def _signature(fixed: list[dict], unresolved: list[dict]) -> str:
    return "|".join(sorted(f"fix:{f['target']}" for f in fixed)
                    + sorted(f"open:{u['target']}:{u['mode']}" for u in unresolved))


def _build_message(ts: str, fixed: list[dict], unresolved: list[dict]) -> str:
    import re
    def _clean(s: str) -> str:
        return re.sub(r"<[^>]+>", "", str(s))
    parts = [f"🔧 <b>JobFinder self-heal</b> ({ts})"]
    if fixed:
        parts.append("\n<b>Восстановлено автоматически:</b>")
        for f in fixed:
            mark = "✅" if f["ok"] else "⚠️"
            parts.append(f"{mark} {_clean(f['name'])} — {f['action']}"
                         + (f" (rc={f['rc']})" if f["rc"] not in (0, None) else ""))
    if unresolved:
        parts.append("\n<b>Требуется вмешательство:</b>")
        for u in unresolved[:12]:
            parts.append(f"🔴 {_clean(u['name'])}: {_clean(u['detail'])[:90]} — {u['note']}")
    return "\n".join(parts)


def run_locked(dry_run: bool = False) -> dict:
    """Re-entrant heal tick: hold the fcntl lock so an overlapping cron tick is a clean no-op. This is
    the shared entry point for both `health_heal` and `health --heal`. Never raises."""
    lock = _acquire_lock()
    if lock is None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} health_heal: another tick holds the lock — exiting", flush=True)
        return {"skipped": "locked"}
    try:
        return heal(dry_run=dry_run)
    except Exception as exc:
        print(f"health_heal error: {type(exc).__name__}: {exc}", flush=True)
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Self-healing health watcher (detect → auto-fix safe modes → alert).")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what WOULD be healed (with each target's cooldown/breaker decision); change nothing")
    args = ap.parse_args()
    res = run_locked(dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
