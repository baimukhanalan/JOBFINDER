"""On-demand Mass-Hiring auto-apply: start the per-lane drivers from the dashboard button/modal
instead of waiting for cron. Each lane reuses its PROVEN driver verbatim (same module + env as the
installed crontab line), overriding --workers / --limit. Also edits the crontab CADENCE of those
lane lines (the «частота» control), preserving every other crontab line.

Spawned subprocesses inherit the dashboard's process group (`mail`) — do NOT re-wrap in `sg mail`."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent.parent)   # /home/projects/jobfinder
LOG_DIR = Path(REPO) / "logs"

# lane -> proven driver, mirroring the installed crontab invocation (module + env + fixed extra args).
LANES: dict[str, dict] = {
    "ttec":            {"label": "TTEC",            "mod": "backend.tools.mass_hiring_apply_taleo_cron",   "limit": True,  "env": {},                       "extra": []},
    "teleperformance": {"label": "Teleperformance", "mod": "backend.tools.mass_hiring_apply_tp_cron",      "limit": True,  "env": {},                       "extra": ["--keep", "8"]},
    "centene":         {"label": "Centene",         "mod": "backend.tools.mass_hiring_apply_workday_cron", "limit": True,  "env": {"WORKDAY_ADVANCE": "1"}, "extra": ["--tenant", "centene"]},
    "sutherland":      {"label": "Sutherland",      "mod": "backend.tools.mass_hiring_apply_sr_cron",      "limit": True,  "env": {},                       "extra": ["--keep", "8"]},
    "kelly":           {"label": "Kelly",           "mod": "backend.tools.mass_hiring_apply_kelly_cron",   "limit": False, "env": {},                       "extra": []},
    "maximus":         {"label": "Maximus",         "mod": "backend.tools.mass_hiring_apply_cron",         "limit": False, "env": {},                       "extra": []},
}
_LOG = {"ttec": "taleo_apply.log", "teleperformance": "tp_apply.log", "centene": "workday_apply.log",
        "sutherland": "sr_apply.log", "kelly": "kelly_apply.log", "maximus": "mh_apply.log"}

# crontab-cadence presets (the «частота» selector). A preset is applied PER-LANE with a distinct
# minute + STAGGERED hours so the 6 mass-hiring lanes never pile headful Chromium onto the single
# Xvfb `:98` at once. Re-creating the 2026-09-12 overload (all lanes at `1,6,11,15,20` → 46 chrome
# procs, load ~7-10, fills dying mid-run with TargetClosedError) is exactly what one shared schedule
# string did — so `set_schedule` derives each selected lane's OWN schedule from the tables below
# (see `schedule_for`), and applying one preset to several lanes can NEVER un-stagger them.
SCHEDULE_LABELS = {"5x": "5×/день", "3x": "3×/день", "2x": "2×/день",
                   "daily": "1×/день", "hourly": "каждый час", "off": "выключить"}
SCHEDULES: tuple[str, ...] = tuple(SCHEDULE_LABELS)   # the valid preset ids

# per-lane MINUTE offset (≥6 min apart, constant across cadences) so two lanes never collide on the
# minute — mirrors the documented live crontab (Maximus 0 · TP 12 · Kelly 24 · SR 36 · Workday 48 ·
# Taleo 54).
_LANE_MIN = {"maximus": 0, "teleperformance": 12, "kelly": 24,
             "sutherland": 36, "centene": 48, "ttec": 54}

# per-lane HOURS for each cadence, spread so ≤2 lanes start any hour. `5x` mirrors the documented
# live phases; the lower cadences keep the same staggering property.
_LANE_HOURS: dict[str, dict[str, str]] = {
    "5x": {"maximus": "0,5,10,15,20", "teleperformance": "1,6,11,16,21",
           "kelly": "2,7,12,17,22", "ttec": "2,8,13,19,23",
           "sutherland": "3,8,13,18,23", "centene": "4,9,14,19,0"},
    "3x": {"maximus": "0,8,16", "teleperformance": "2,10,18", "kelly": "4,12,20",
           "ttec": "6,14,22", "sutherland": "1,9,17", "centene": "3,11,19"},
    "2x": {"maximus": "0,12", "teleperformance": "2,14", "kelly": "4,16",
           "ttec": "6,18", "sutherland": "8,20", "centene": "10,22"},
    "daily": {"maximus": "4", "teleperformance": "6", "kelly": "8",
              "ttec": "10", "sutherland": "12", "centene": "14"},
    "hourly": {"maximus": "*", "teleperformance": "*", "kelly": "*",
               "ttec": "*", "sutherland": "*", "centene": "*"},
}


def schedule_for(lane: str, preset: str) -> str | None:
    """The 5-field crontab schedule for ONE lane under a cadence preset. None → 'off' / unknown
    (the caller comments the line out). Each lane gets its OWN minute + staggered hours, so applying
    a preset to many lanes at once keeps them spread across the day (the anti-overload invariant)."""
    if preset == "off" or preset not in _LANE_HOURS:
        return None
    hours = _LANE_HOURS[preset].get(lane)
    if hours is None:
        return None
    return f"{_LANE_MIN.get(lane, 0)} {hours} * * *"

_LOCK = threading.RLock()
_RUN: dict = {"active": False, "started": 0, "count": None, "workers": 0, "lanes": {}}
_PROCS: dict = {}   # lane -> Popen


def _spawn(lane: str, workers: int, count: int | None) -> subprocess.Popen:
    spec = LANES[lane]
    argv = [sys.executable, "-m", spec["mod"], "--workers", str(workers)] + list(spec["extra"])
    if spec["limit"] and count:
        argv += ["--limit", str(count)]
    env = dict(os.environ)
    env["DISPLAY"] = ":98"
    env.update(spec["env"])
    LOG_DIR.mkdir(exist_ok=True)
    logf = open(LOG_DIR / _LOG.get(lane, f"{lane}_apply.log"), "a")   # noqa: SIM115 - lives with the proc
    logf.write(f"\n===== on-demand start {time.strftime('%Y-%m-%d %H:%M:%S')} "
               f"workers={workers} count={count} =====\n")
    logf.flush()
    return subprocess.Popen(argv, cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT,
                            start_new_session=True)


def start(lanes, workers: int = 2, count: int | None = None) -> dict:
    with _LOCK:
        if _RUN["active"] and any(p.poll() is None for p in _PROCS.values()):
            return {"ok": False, "error": "уже идёт", **_status_locked()}
        lanes = [l for l in (lanes or []) if l in LANES] or list(LANES)
        workers = max(1, min(8, int(workers or 2)))
        _PROCS.clear()
        _RUN.update({"active": True, "started": int(time.time()), "count": count,
                     "workers": workers, "lanes": {}})
        for l in lanes:
            try:
                p = _spawn(l, workers, count)
                _PROCS[l] = p
                _RUN["lanes"][l] = {"label": LANES[l]["label"], "pid": p.pid,
                                    "state": "running", "rc": None}
            except Exception as e:   # a bad lane must not abort the others
                _RUN["lanes"][l] = {"label": LANES[l]["label"], "pid": None,
                                    "state": "error", "rc": None, "error": str(e)[:180]}
        return {"ok": True, **_status_locked()}


def _status_locked() -> dict:
    any_alive = False
    for l, p in _PROCS.items():
        rc = p.poll()
        st = _RUN["lanes"].get(l, {})
        if rc is None:
            st["state"] = "running"
            any_alive = True
        else:
            st["state"] = "done" if rc == 0 else "error"
            st["rc"] = rc
        _RUN["lanes"][l] = st
    if _RUN["active"] and _PROCS and not any_alive:
        _RUN["active"] = False
    return {"active": _RUN["active"], "started": _RUN["started"], "count": _RUN["count"],
            "workers": _RUN["workers"], "lanes": _RUN["lanes"],
            "elapsed": (int(time.time()) - _RUN["started"]) if _RUN["started"] else 0}


def status() -> dict:
    with _LOCK:
        return _status_locked()


def stop() -> dict:
    with _LOCK:
        for p in _PROCS.values():
            try:
                if p.poll() is None:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        _RUN["active"] = False
        return _status_locked()


# ---- crontab cadence editor (the «частота» control) --------------------------------------------
def _modbase(lane: str) -> str:
    return LANES[lane]["mod"].split(".")[-1]   # e.g. mass_hiring_apply_taleo_cron


def _read_crontab() -> list[str]:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return (r.stdout or "").splitlines() if r.returncode == 0 else []


_CRON_PREFIX = re.compile(r"^\s*(#\s*)?((?:\S+\s+){5})(.*)$")


def get_schedules() -> dict:
    """lane -> current crontab schedule string (or 'off' if the line is commented / missing)."""
    lines = _read_crontab()
    out = {}
    for lane in LANES:
        mb = _modbase(lane)
        cur = "off"
        for ln in lines:
            if mb not in ln:
                continue
            m = _CRON_PREFIX.match(ln)
            if not m:
                continue
            cur = "off" if m.group(1) else m.group(2).strip()
            break
        out[lane] = cur
    return out


def set_schedule(lanes, preset: str) -> dict:
    """Rewrite ONLY the leading cron schedule of the selected lanes' lines (never add/remove lines),
    STAGGERED per lane: each matched line gets its OWN lane's `schedule_for(lane, preset)` — NOT one
    shared string (that shared string re-created the 2026-09-12 `:98` overload). 'off' comments the
    line out; a real preset uncomments + sets that lane's staggered schedule. Backed up first."""
    if preset not in SCHEDULES:
        return {"ok": False, "error": "unknown preset"}
    lanes = [l for l in (lanes or list(LANES)) if l in LANES]
    lines = _read_crontab()
    if not lines:
        return {"ok": False, "error": "empty crontab"}
    # modbase -> lane, so a matched line is attributed to its lane and gets that lane's schedule.
    # (modbases are mutually non-substring, so exactly one matches any lane line.)
    mb_to_lane = {_modbase(l): l for l in lanes}
    changed = 0
    out = []
    for ln in lines:
        lane = next((la for mb, la in mb_to_lane.items() if mb in ln), None)
        if lane is None:
            out.append(ln)
            continue
        m = _CRON_PREFIX.match(ln)
        if not m:
            out.append(ln)
            continue
        body = m.group(3)
        sched = schedule_for(lane, preset)
        if sched is None:                       # off -> comment out (idempotent)
            out.append(ln if ln.lstrip().startswith("#") else "# " + ln)
        else:
            out.append(f"{sched} {body}")       # set THIS lane's staggered schedule + uncomment
        changed += 1
    try:
        LOG_DIR.mkdir(exist_ok=True)
        (LOG_DIR / f"crontab.bak-{int(time.time())}").write_text("\n".join(lines) + "\n")
        subprocess.run(["crontab", "-"], input="\n".join(out) + "\n", text=True, check=True)
    except Exception as e:
        return {"ok": False, "error": str(e)[:180]}
    return {"ok": True, "changed": changed, "preset": preset}
