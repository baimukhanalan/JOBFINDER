"""System health snapshot for the admin dashboard (`/health`).

Gathers, read-only + best-effort (every check wrapped so one failure never breaks the page):
  * pm2 services (the jobfinder-* long-running processes) — status / uptime / restarts / cpu / mem
  * cron lanes — each known crontab line's log file: when it last ran + a hint of the last result,
    flagged STALE past its expected cadence
  * data stores — Postgres jobfinder_crm reachability, the assessment bank + answer-key coverage
  * runtime — the :98 headful display stack (Xvfb / x11vnc / noVNC), load average, disk, memory

Each check returns {name, status: ok|warn|down, detail, ...}. No writes, no external calls.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
_LOGS = os.path.join(_ROOT, "logs")


def _age_str(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _tail_lines(path: str, n: int = 12, maxlen: int = 200) -> list[str]:
    """The last `n` non-empty lines of a log (best-effort, reads only an 8 KB tail). Scanning a block
    — not just the final line — catches a run that crashed but printed a benign-looking last line."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            chunk = f.read().decode("utf-8", "replace")
        lines = [ln.strip()[:maxlen] for ln in chunk.splitlines() if ln.strip()]
        return lines[-n:]
    except Exception:
        return []


def _tail_line(path: str, maxlen: int = 160) -> str:
    """Last non-empty line of a log (best-effort)."""
    lines = _tail_lines(path, n=1, maxlen=maxlen)
    return lines[-1] if lines else ""


# ---- pm2 -----------------------------------------------------------------------------------------
def pm2_services() -> list[dict]:
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=15).stdout
        procs = json.loads(out)
    except Exception as exc:
        return [{"name": "pm2", "status": "down", "detail": f"pm2 jlist failed: {str(exc)[:80]}"}]
    rows = []
    for p in procs:
        name = p.get("name", "?")
        if not name.startswith("jobfinder"):
            continue
        env = p.get("pm2_env", {}) or {}
        st = env.get("status", "?")
        up = env.get("pm_uptime")
        restarts = env.get("restart_time", 0)
        monit = p.get("monit", {}) or {}
        cpu = monit.get("cpu", 0)
        mem = (monit.get("memory", 0) or 0) / (1024 * 1024)
        uptime = _age_str((time.time() * 1000 - up) / 1000) if up else "?"
        status = "ok" if st == "online" else ("warn" if st in ("launching", "one-launch-status") else "down")
        if st == "online" and restarts and restarts > 20:
            status = "warn"
        rows.append({"name": name, "status": status,
                     "detail": f"{st} · up {uptime} · {int(restarts)} restarts · cpu {cpu}% · {mem:.0f}MB"})
    if not rows:
        rows.append({"name": "pm2", "status": "warn", "detail": "no jobfinder-* processes found"})
    return rows


# ---- cron lanes ----------------------------------------------------------------------------------
# (label, log filename under logs/, max age in HOURS before it's considered STALE)
_CRONS = [
    ("Каталог: сбор (nightly)", "catalog.log", 30),
    ("Каталог: регионы", "regions.log", 30),
    ("Каталог: формы", "forms.log", 30),
    ("Каталог: est-comp", "est_comp.log", 30),
    ("Компании: discovery (weekly)", "discovery.log", 24 * 8),
    ("Mass Hiring: сбор", "masshiring.log", 8),      # cron every 6h since 2026-09-09 (no manual button)
    ("Прокси: Bright Data (daily)", "brightdata.log", 30),
    ("Почта: retention", "retention.log", 30),
    ("Почта: health-probe", "health.log", 1),
    ("Prefill retention", "prefill_retention.log", 30),
    ("Apply: Maximus (5×/день)", "mh_apply.log", 8),
    ("Apply: Teleperformance", "tp_apply.log", 8),
    ("Apply: Taleo/TTEC", "taleo_apply.log", 8),
    ("Apply: Kelly", "kelly_apply.log", 8),
    ("Apply: SmartRecruiters", "sr_apply.log", 8),
    ("Apply: Workday/Centene", "workday_apply.log", 8),
    ("Харвестер вопросов (hourly)", "harvest_cron.log", 3),
]
# Error signal in a log tail. `\w*error` matches bare "error" AND CamelCase endings
# (ModuleNotFoundError / KeyError / TimeoutError / …); "no module named" catches the import failure
# whose CamelCase word `\berror\b` used to miss (the harvest-cron incident, 2026-09-09).
_ERR_RE = re.compile(
    r"(?:\w*error|traceback|exception|failed|no fresh|no module named|"
    r"ne500|state_transition|modulenotfound)", re.I)
# Benign phrases that CONTAIN an error word but mean success — apply lanes end with a summary like
# "…errors=0" / "0 errors", and a "still going — exiting" flock-skip is normal. Don't flag those.
_BENIGN_RE = re.compile(r"errors?\s*[=:]\s*0\b|\b0\s+errors?\b|still going\s*[—-]\s*exiting", re.I)


# A run-completion summary line — every cron here ends a successful run on one of these shapes
# (`DONE …`, `FINISHED …`, `stats: {…}`/`collect: {…}`, `catalog counts -> …`, or a bare JSON/dict
# summary). Seen as the newest line it means "the latest run completed", whatever sits above it.
_SUCCESS_RE = re.compile(r"^\s*(?:DONE\b|FINISHED\b|stats:|collect:|catalog counts|\{)", re.I)


def cron_lanes() -> list[dict]:
    rows = []
    for label, fn, max_h in _CRONS:
        path = os.path.join(_LOGS, fn)
        if not os.path.exists(path):
            rows.append({"name": label, "status": "warn", "detail": "нет лога (ещё не запускался?)"})
            continue
        try:
            age = time.time() - os.path.getmtime(path)
        except Exception:
            age = 0
        tail = _tail_lines(path)
        last = tail[-1] if tail else ""
        stale = age > max_h * 3600
        # A FAILED run ends on its error; a run that completed ends on its summary line. Walk the last
        # few lines NEWEST-first: the first completion summary (or a benign "errors=0" / flock-skip
        # line) means the latest run is alive — anything older belongs to a PRIOR run (e.g. yesterday's
        # traceback still sitting 2 lines under today's `collect:`/`stats:`, which used to keep the
        # lane red after it had recovered); the first error line before any summary = failed.
        err = False
        for ln in reversed(tail[-4:]):
            if not ln.strip():
                continue
            if _SUCCESS_RE.search(ln) or _BENIGN_RE.search(ln):
                break
            if _ERR_RE.search(ln):
                err = True
                break
        # A lane that hasn't written ANYTHING for 2× its cadence is HUNG or never started — RED too.
        # A stuck cron writes no error line at all (the 2026-09-07..09 DB-lock outage: 7 lanes sat
        # silently in a lock queue for ~2 days and only ever showed as yellow "stale"), so silence
        # past 2× the cadence must escalate and alert, not idle as a benign warn.
        hung = age > 2 * max_h * 3600
        # An ERRORED lane is RED (down) so it drives the overall badge red and can't hide among the
        # benign yellow "stale-between-runs" lanes; a merely-stale (but not errored) lane stays warn.
        status = "down" if (err or hung) else ("warn" if stale else "ok")
        note = "ОШИБКА · " if err else ("ЗАВИС/НЕ ЗАПУСКАЛСЯ · " if hung else ("STALE · " if stale else ""))
        rows.append({"name": label, "status": status,
                     "detail": f"{note}last run {_age_str(age)} · {last or '—'}"})
    return rows


# ---- data stores ---------------------------------------------------------------------------------
def postgres() -> dict:
    try:
        from backend.tools import mail_db
        with mail_db.conn() as c:      # context-managed pooled connection
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute("SELECT count(*) FROM mail_index")
            n = cur.fetchone()[0]
            cur.close()
        return {"name": "Postgres jobfinder_crm", "status": "ok", "detail": f"reachable · mail_index {n} rows"}
    except Exception as exc:
        return {"name": "Postgres jobfinder_crm", "status": "down", "detail": f"unreachable: {str(exc)[:90]}"}


def bank_stats() -> dict:
    try:
        from backend.tools.assessment_harvester import bank, answer_key
        bank.reload()
        total = bank.size()
        cov = answer_key.coverage()
        path = bank._BANK_PATH
        age = _age_str(time.time() - os.path.getmtime(path)) if os.path.exists(path) else "?"
        keyed, mcq = cov.get("keyed", 0), cov.get("mcq", 0)
        status = "ok" if (mcq and keyed / max(1, mcq) >= 0.9) else "warn"
        return {"name": "Банк вопросов + ключ ответов", "status": status,
                "detail": f"{total} вопросов · ключ {keyed}/{mcq} MCQ · обновлён {age}"}
    except Exception as exc:
        return {"name": "Банк вопросов + ключ ответов", "status": "warn", "detail": f"n/a: {str(exc)[:90]}"}


# ---- runtime -------------------------------------------------------------------------------------
def _pgrep(pat: str) -> int:
    try:
        out = subprocess.run(["pgrep", "-fc", pat], capture_output=True, text=True, timeout=8).stdout.strip()
        return int(out or "0")
    except Exception:
        return 0


def display_stack() -> list[dict]:
    checks = [("Xvfb :98", "Xvfb.*:98"), ("x11vnc :5901", "x11vnc"), ("noVNC :6090", "websockify|novnc")]
    rows = []
    for label, pat in checks:
        n = _pgrep(pat)
        rows.append({"name": label, "status": "ok" if n else "down",
                     "detail": f"{n} proc" if n else "не запущен"})
    return rows


def system() -> list[dict]:
    rows = []
    try:
        l1, l5, l15 = os.getloadavg()
        cores = os.cpu_count() or 1
        st = "ok" if l1 < cores * 1.5 else ("warn" if l1 < cores * 3 else "down")
        rows.append({"name": "Load average", "status": st, "detail": f"{l1:.1f} / {l5:.1f} / {l15:.1f} ({cores} cores)"})
    except Exception:
        pass
    try:
        du = shutil.disk_usage(_ROOT)
        pct = du.used / du.total * 100
        st = "ok" if pct < 85 else ("warn" if pct < 95 else "down")
        rows.append({"name": "Диск (/)", "status": st,
                     "detail": f"{pct:.0f}% занято · {du.free / 1e9:.0f} GB свободно"})
    except Exception:
        pass
    try:
        mt = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                mt[k.strip()] = int(v.split()[0]) if v.split() else 0
        total, avail = mt.get("MemTotal", 1), mt.get("MemAvailable", 0)
        pct = (1 - avail / total) * 100
        st = "ok" if pct < 85 else ("warn" if pct < 95 else "down")
        rows.append({"name": "Память", "status": st,
                     "detail": f"{pct:.0f}% занято · {avail / 1e6:.1f} GB свободно"})
    except Exception:
        pass
    return rows


def gather() -> dict:
    """Full health snapshot: a list of sections, each {title, rows[]}. Best-effort per section."""
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception as exc:
            return [{"name": fn.__name__, "status": "warn", "detail": f"check failed: {str(exc)[:80]}"}]

    sections = [
        {"title": "Сервисы (pm2)", "rows": safe(pm2_services)},
        {"title": "Кроны", "rows": safe(cron_lanes)},
        {"title": "Данные", "rows": [safe_one(postgres), safe_one(bank_stats)]},
        {"title": "Дисплей :98 (headful)", "rows": safe(display_stack)},
        {"title": "Система", "rows": safe(system)},
    ]
    counts = {"ok": 0, "warn": 0, "down": 0}
    for sec in sections:
        for r in sec["rows"]:
            counts[r.get("status", "warn")] = counts.get(r.get("status", "warn"), 0) + 1
    overall = "down" if counts["down"] else ("warn" if counts["warn"] else "ok")
    return {"sections": sections, "counts": counts, "overall": overall,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


def safe_one(fn) -> dict:
    try:
        return fn()
    except Exception as exc:
        return {"name": fn.__name__, "status": "warn", "detail": f"check failed: {str(exc)[:80]}"}


# ---- auto-alert (push, so nobody has to open /health to notice a failure) -----------------------
# Own throttle state (kept separate from mail_health's so its recovery-key logic isn't disturbed).
_ALERT_STATE = os.path.join(_LOGS, "health_alert_state.json")


def _tg(text: str) -> bool:
    try:
        import httpx

        from backend.config import settings
        tok, chat = settings.telegram_bot_token, settings.telegram_chat_id
        if not tok or not chat:
            return False
        r = httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                       data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": "true"})
        return r.status_code < 300
    except Exception:
        return False


def check_and_alert(cooldown: int = 14400) -> dict:
    """Run gather(); Telegram the owner when anything is DOWN (throttled per `cooldown`), and send ONE
    recovery note when it clears. This is the PUSH the pull-only /health tab lacked — a failing service
    or cron now pings the owner within a cron tick instead of waiting to be noticed. Never raises."""
    snap = gather()
    down = [f"{r.get('name')}: {r.get('detail', '')}"
            for sec in snap["sections"] for r in sec["rows"] if r.get("status") == "down"]
    try:
        st = json.loads(open(_ALERT_STATE, encoding="utf-8").read())
    except Exception:
        st = {}
    now = int(time.time())

    def _save(s: dict) -> None:
        try:
            os.makedirs(_LOGS, exist_ok=True)
            with open(_ALERT_STATE, "w", encoding="utf-8") as f:
                json.dump(s, f)
        except Exception:
            pass

    if down:
        if now - int(st.get("last", 0)) >= cooldown:
            body = (f"🔴 <b>JobFinder health</b> — {len(down)} проблем ({snap['ts']})\n\n"
                    + "\n".join("• " + d for d in down[:12]))
            _tg(body)
            print(f"[health] ALERT: {len(down)} down: {down}", flush=True)
        _save({"last": now if now - int(st.get("last", 0)) >= cooldown else st.get("last", 0),
               "active": True})
    else:
        if st.get("active"):
            _tg(f"🟢 <b>JobFinder health</b> — всё восстановилось ({snap['ts']})")
            print("[health] RECOVERED", flush=True)
        _save({"last": st.get("last", 0), "active": False})
    return {"overall": snap["overall"], "down": down}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Health snapshot + optional Telegram alert.")
    ap.add_argument("--alert", action="store_true",
                    help="check and Telegram the owner if anything is DOWN (cron entry point)")
    ap.add_argument("--cooldown", type=int, default=14400, help="alert throttle seconds (default 1800)")
    args = ap.parse_args()
    if args.alert:
        res = check_and_alert(cooldown=args.cooldown)
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(json.dumps(gather(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
