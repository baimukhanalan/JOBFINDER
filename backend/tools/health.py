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


def _tail_line(path: str, maxlen: int = 160) -> str:
    """Last non-empty line of a log (best-effort, reads only the tail)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            chunk = f.read().decode("utf-8", "replace")
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        return (lines[-1] if lines else "")[:maxlen]
    except Exception:
        return ""


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
    ("Mass Hiring: сбор", "masshiring.log", 30),
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
_ERR_RE = re.compile(r"\b(error|traceback|exception|failed|no fresh|ne500|state_transition)\b", re.I)


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
        last = _tail_line(path)
        stale = age > max_h * 3600
        err = bool(_ERR_RE.search(last))
        status = "warn" if (stale or err) else "ok"
        note = "STALE · " if stale else ""
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
