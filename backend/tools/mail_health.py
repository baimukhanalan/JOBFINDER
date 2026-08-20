"""Health signalling for the mail index.

The CRM falls back to a live Maildir scan when Postgres is unreachable — good for
resilience, but that fallback can silently HIDE a dead indexer / down database. This
module makes failures visible:

  * the indexer calls heartbeat() every reconcile sweep;
  * the CRM calls record_fallback() whenever it drops to a live scan;
  * a cron runs check() every 10 min: pings Postgres + verifies the heartbeat is
    fresh, and sends ONE throttled Telegram alert when unhealthy (plus a recovery
    note when it's healthy again).

Reuses the project's Telegram creds (config.settings.telegram_*). Alerts are
throttled per key (default 30 min) via a small state file so an outage can't spam.

    python -m backend.tools.mail_health check      # cron entry point
    python -m backend.tools.mail_health test       # send a one-off test alert
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

from backend.config import settings

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "uploads" / "mail_health_state.json"
HEARTBEAT = ROOT / "uploads" / ".mail_indexer_heartbeat"
COOLDOWN = 1800          # 30 min between repeats of the same alert
STALE_AFTER = 900        # heartbeat older than 15 min => indexer dead/hung


def heartbeat() -> None:
    """Called by the indexer each reconcile sweep — proves the backstop is alive."""
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(str(int(time.time())))
    except OSError:
        pass


def _tg(text: str) -> bool:
    tok, chat = settings.telegram_bot_token, settings.telegram_chat_id
    if not tok or not chat:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                       data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"})
        return r.status_code < 300
    except Exception:
        return False


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(s: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(s))
    except OSError:
        pass


def notify(key: str, text: str, cooldown: int = COOLDOWN) -> bool:
    """Send a throttled alert (once per `cooldown` per key). Marks the key active so
    check() can send a recovery note later. Returns True if a message actually went."""
    s = _state()
    last = s.get("last", {})
    active = set(s.get("active", []))
    active.add(key)
    now = int(time.time())
    fired = False
    if now - int(last.get(key, 0)) >= cooldown:
        last[key] = now
        fired = True
        print(f"[mail-health] ALERT {key}: {text}", flush=True)
        _tg(text)
    s["last"] = last
    s["active"] = sorted(active)
    _save(s)
    return fired


def record_fallback(where: str) -> None:
    """Called from the CRM's DB-first read paths when they drop to a live scan."""
    notify("index_fallback",
           f"⚠️ <b>Mail index unavailable</b> — CRM fell back to a live disk scan "
           f"(<code>{where}</code>). Check Postgres <code>jobfinder_crm</code> and the "
           f"<code>jobfinder-mail-indexer</code> pm2 process.")


def _clear(recovery_text: str) -> None:
    s = _state()
    if s.get("active"):
        print("[mail-health] recovered", flush=True)
        _tg(recovery_text)
    _save({})  # reset both active + cooldown history so the next issue alerts at once


def check() -> list:
    """Cron entry: return a list of problems (empty = healthy) and alert on change."""
    problems = []
    try:
        from backend.tools import mail_db
        mail_db.counts()
    except Exception as e:
        problems.append(f"Postgres jobfinder_crm unreachable: {str(e)[:120]}")
    try:
        age = int(time.time()) - int(HEARTBEAT.read_text().strip())
        if age > STALE_AFTER:
            problems.append(f"indexer heartbeat stale ({age // 60} min) — "
                            f"jobfinder-mail-indexer dead or hung")
    except Exception:
        problems.append("indexer heartbeat missing — jobfinder-mail-indexer not running?")

    if problems:
        notify("index_health", "🔴 <b>Mail index unhealthy</b>\n"
               + "\n".join("• " + p for p in problems))
    else:
        _clear("🟢 <b>Mail index recovered</b> — Postgres + indexer healthy again.")
    return problems


def dashboard_warning() -> str | None:
    """A short RU message for an in-dashboard banner when the mail index is unhealthy
    — a delivery-independent signal (no Telegram needed). Cheap: just reads the
    heartbeat file. A stale/missing heartbeat means the indexer (and usually Postgres)
    is down, so the CRM is on the slow fallback path."""
    try:
        age = int(time.time()) - int(HEARTBEAT.read_text().strip())
    except Exception:
        return ("Индекс почты не инициализирован — работаю в прямом режиме (медленно). "
                "Проверьте процесс jobfinder-mail-indexer.")
    if age > STALE_AFTER:
        return (f"Индекс почты не обновлялся {age // 60} мин — возможно, индексатор упал. "
                "Работаю в прямом режиме (медленно).")
    return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "test":
        ok = _tg("🔔 <b>Mail-health test</b> — alerting is wired and working. "
                 "You'll only hear from this if the mail index goes down.")
        print(json.dumps({"test_sent": ok}))
    else:
        probs = check()
        print(json.dumps({"healthy": not probs, "problems": probs}))
