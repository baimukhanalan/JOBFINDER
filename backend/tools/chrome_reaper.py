"""Reap LEAKED Chromium processes so the apply/assessment lanes don't exhaust the box.

The lanes launch headful Chromium on DISPLAY=:98; a crashed fill can leave chrome procs whose
parent died — they REPARENT to init (ppid==1) and accumulate. Proven 2026-09-12: 13 chrome procs
that were 6.6 DAYS old, load ~7-10, live fills dying mid-run with `TargetClosedError`.

SAFETY: this kills ONLY ORPHANS (ppid==1) older than --min-age. A LIVE fill's browser is a child
of its co-pilot/worker process (ppid != 1) — never an orphan — so it is NEVER touched, at any age
(the persistent co-pilot browser included). A chrome only becomes an orphan once the thing that
launched it is gone, i.e. it is genuinely leaked. Guarded end-to-end; never raises. Cron: */30.
    python3 -m backend.tools.chrome_reaper [--min-age 3600] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time

# match the chromium family (renderers, gpu, crashpad, headless shell)
_NAMES = ("chrome", "chromium", "headless_shell")


def _procs():
    """[(pid, ppid, age_seconds, comm)] for every process; [] on any failure."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,etimes=,comm="],
                             capture_output=True, text=True, timeout=15)
        rows = []
        for ln in out.stdout.splitlines():
            p = ln.split(None, 3)
            if len(p) < 4:
                continue
            try:
                pid, ppid, age = int(p[0]), int(p[1]), int(p[2])
            except ValueError:
                continue
            rows.append((pid, ppid, age, p[3].strip()))
        return rows
    except Exception:
        return []


def leaked(rows, min_age: int):
    """Orphaned (ppid==1) chromium procs at least `min_age` seconds old. Excludes self."""
    me = os.getpid()
    out = []
    for pid, ppid, age, comm in rows:
        if pid == me:
            continue
        if not any(n in comm.lower() for n in _NAMES):
            continue
        if ppid == 1 and age >= min_age:      # ORPHAN only -> a live fill's browser (ppid!=1) is safe
            out.append((pid, ppid, age, comm))
    return out


def reap(min_age: int = 3600, dry_run: bool = False) -> dict:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    victims = leaked(_procs(), min_age)
    if not victims:
        print(f"{ts} chrome_reaper: no leaked (orphan >{min_age}s) chrome to reap", flush=True)
        return {"found": 0, "killed": 0}
    killed = 0
    for pid, ppid, age, comm in victims:
        if dry_run:
            print(f"{ts} WOULD kill pid={pid} ppid={ppid} age={age}s comm={comm}", flush=True)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
            print(f"{ts} killed orphan pid={pid} age={age}s comm={comm}", flush=True)
        except ProcessLookupError:
            pass
        except Exception as exc:
            print(f"{ts} kill {pid} failed: {type(exc).__name__}", flush=True)
    print(f"{ts} chrome_reaper: {'would kill' if dry_run else 'killed'} "
          f"{killed if not dry_run else len(victims)}/{len(victims)}", flush=True)
    return {"found": len(victims), "killed": killed}


def main() -> None:
    ap = argparse.ArgumentParser(description="Reap orphaned (leaked) Chromium processes")
    ap.add_argument("--min-age", type=int, default=3600,
                    help="only reap orphans at least this many seconds old (default 3600)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be killed, kill nothing")
    a = ap.parse_args()
    try:
        reap(min_age=a.min_age, dry_run=a.dry_run)
    except Exception as exc:
        print(f"chrome_reaper error: {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    main()
