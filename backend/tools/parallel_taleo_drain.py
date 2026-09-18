"""Parallel drain of the `taleo_ttec` (TTEC → Harver) assessment backlog on ONE server (Xvfb :98).

The single sequential `harvest_runner --platform taleo_ttec --limit N` drains ~2-3/hr (each Harver
battery is 20-30 min of timed modules). This driver runs N LANES concurrently for ~N× throughput.

Why it's safe to parallelise (empirically verified 2026-09-18, 2 concurrent Harver drives):
  * CAMERA — the v4l2loopback `/dev/video0` broadcasts ONE output feed to MANY capture openers
    (module `max_openers=10`); two concurrent Harver "Test your camera" checks BOTH passed on the
    shared device while another browser also read it. So per-lane video devices are NOT needed (and
    are unsafe here: adding video1..N needs a `modprobe -r v4l2loopback` reload that would destroy
    /dev/video0 out from under the live Sutherland/AMCAT runs). The ONLY hazard is multiple WRITERS
    (each process spawning its own ffmpeg feeder corrupts the single-writer format), so this driver
    starts ONE shared feeder (camera_daemon) and every lane runs as a `CAMERA_SHARED_READER` (never
    spawns a competing feeder — see camera.ensure).
  * MIC — each lane gets its OWN pulse null-sink via `HARVEST_MIC_SUFFIX` (mic.py) + a getUserMedia
    audio-source pin (core `_MIC_PIN_JS`), so concurrent speaking/SVAR modules never garble. (Harver
    barely mic-checks, but this keeps it clean; `--shared-mic` disables it.)
  * NO DOUBLE-DRIVE — a flock'd claim file hands each still-pending invite to exactly one lane
    (adapted from the scratchpad `mac_workers.sh` atomic-claim pattern). `discover.discover` already
    excludes tokens recorded in `harvest_state.json`; each drive runs `harvest_runner --url …
    --record-state` so a completed/attempted single-use token is never re-served (a restart resumes
    cleanly), and `--mailbox <full@takhet.com>` marks the CRM invite «пройдено» on a real completion.

Run (from the repo root, under the mail group + the X display the headful browsers need):
    cd /home/projects/jobfinder && DISPLAY=:98 sg mail -c \
        'PYTHONPATH=. python3 -m backend.tools.parallel_taleo_drain --lanes 4'

Each lane logs to `logs/taleo_lane_<i>.log`; the driver's own progress goes to stdout. Stop the whole
drain with `pkill -f parallel_taleo_drain` (kills the driver + its lane subprocesses).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester import discover  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.path.join(REPO, "logs")
CLAIM_FILE = os.path.join(LOG_DIR, "taleo_drain_claimed.tsv")
PLATFORM = "taleo_ttec"

_claim_lock = threading.Lock()   # in-process guard (threads share the fd; the flock adds cross-process)
_counts_lock = threading.Lock()
_counts: dict[str, int] = {"claimed": 0, "completed": 0, "other": 0, "timeout": 0, "error": 0}


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [drain] {msg}", flush=True)


def _discover_pending() -> list[tuple[str, str]]:
    """Current still-pending invites (excludes any token already in harvest_state), retried on a DB
    blip so a transient error never empties a lane's queue prematurely."""
    for attempt in range(4):
        try:
            return discover.discover(PLATFORM, limit=None, include_done=False)
        except Exception as exc:  # DB hiccup / pool exhaustion — back off and retry
            _log(f"discover error (try {attempt + 1}): {str(exc)[:120]}")
            time.sleep(3 + 3 * attempt)
    return []


def _claim_next() -> tuple[str, str] | None:
    """Atomically hand the next unclaimed pending invite to this lane. flock'd RMW of the claim file
    (cross-process safe) under the in-process lock (thread safe). Returns (localpart, url) or None."""
    with _claim_lock:
        with open(CLAIM_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                claimed = {ln.rstrip("\n").split("\t")[-1] for ln in f if ln.strip()}
                for mbx, url in _discover_pending():
                    if url in claimed:
                        continue
                    f.write(f"{int(time.time())}\t{mbx}\t{url}\n")
                    f.flush()
                    os.fsync(f.fileno())
                    return mbx, url
                return None
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _lane_env(lane: int, session_secs: int, shared_mic: bool) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO
    env.setdefault("DISPLAY", ":98")
    env["CAMERA_SHARED_READER"] = "1"        # never spawn a competing /dev/video0 feeder
    env["HARVEST_LOCK_SUFFIX"] = f"tl{lane}"  # per-lane single-instance lock in harvest_runner
    env["HARVEST_SESSION_SECS"] = str(session_secs)
    if shared_mic:
        env.pop("HARVEST_MIC_SUFFIX", None)  # all lanes share the one virtmic (proven to work)
    else:
        env["HARVEST_MIC_SUFFIX"] = f"tl{lane}"  # per-lane pulse null-sink + the core mic pin
    return env


def _drive_one(lane: int, mbx: str, url: str, log, env: dict, hard_timeout: int) -> str:
    """Run ONE invite to completion in an isolated harvest_runner subprocess (inherits the per-lane
    env). Returns a short status string. A hard timeout kills a wedged drive and records the token so
    it is never re-served."""
    full = mbx if "@" in mbx else f"{mbx}@takhet.com"
    cmd = [sys.executable, "-m", "backend.tools.harvest_runner",
           "--platform", PLATFORM, "--url", url, "--mailbox", full, "--record-state"]
    log.write(f"\n{time.strftime('%H:%M:%S')} [lane {lane}] START {full}\n{url}\n")
    log.flush()
    try:
        p = subprocess.run(cmd, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                           timeout=hard_timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        log.write(f"{time.strftime('%H:%M:%S')} [lane {lane}] HARD TIMEOUT {full} — killed\n")
        log.flush()
        try:
            discover.mark(url, "lane_timeout")   # burn it so no lane re-serves the wedged token
        except Exception:
            pass
        return "timeout"
    # harvest_runner already recorded the real status into harvest_state (--record-state) + marked the
    # CRM done on a completion; grep the log's last SUMMARY line only for our own tally.
    st = "other"
    try:
        with open(log.name, "rb") as r:
            r.seek(max(0, os.path.getsize(log.name) - 4000))
            tail = r.read().decode("utf-8", "ignore")
        if "status : completed" in tail:
            st = "completed"
        elif rc != 0:
            st = "error"
    except Exception:
        pass
    return st


def _lane(lane: int, session_secs: int, hard_timeout: int, shared_mic: bool) -> None:
    env = _lane_env(lane, session_secs, shared_mic)
    log_path = os.path.join(LOG_DIR, f"taleo_lane_{lane}.log")
    log = open(log_path, "a", buffering=1)
    _log(f"lane {lane} up (mic={'shared' if shared_mic else f'tl{lane}'}, log={log_path})")
    done = 0
    while True:
        claim = _claim_next()
        if claim is None:
            _log(f"lane {lane}: queue drained after {done} drive(s) — exiting")
            break
        mbx, url = claim
        with _counts_lock:
            _counts["claimed"] += 1
        _log(f"lane {lane} -> {mbx} (drive #{done + 1})")
        st = _drive_one(lane, mbx, url, log, env, hard_timeout)
        done += 1
        with _counts_lock:
            _counts[st if st in _counts else "other"] += 1
            tot = _counts["completed"]
        _log(f"lane {lane}: {mbx} -> {st}  (lane {done}; run completed={tot})")
    log.close()


def _ensure_camera_daemon() -> None:
    """Start ONE shared v4l2loopback feeder (camera_daemon) so lanes reuse it instead of each spawning
    a competing writer. No-op if it's already alive."""
    from backend.tools.assessment_harvester import camera
    if camera._daemon_alive():
        _log("camera daemon already alive — reusing its shared feed")
        return
    _log("starting camera_daemon (shared /dev/video0 feeder)…")
    subprocess.Popen([sys.executable, "-m", "backend.tools.assessment_harvester.camera_daemon"],
                     cwd=REPO, env=dict(os.environ, PYTHONPATH=REPO),
                     stdout=open(os.path.join(LOG_DIR, "camera_daemon.log"), "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(20):
        time.sleep(1)
        if camera._daemon_alive() and os.path.exists(camera.DEVICE):
            _log(f"camera daemon up (device {camera.DEVICE})")
            return
    _log("WARNING: camera daemon did not confirm alive in 20s — lanes will still try the device")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lanes", type=int, default=4, help="concurrent Harver lanes (start 4, up to ~6)")
    ap.add_argument("--session-secs", type=int, default=2100,
                    help="per-drive wall-clock (HARVEST_SESSION_SECS); a full Harver battery ~20-30min")
    ap.add_argument("--hard-timeout", type=int, default=2400,
                    help="hard subprocess kill for a wedged drive (> session-secs)")
    ap.add_argument("--stagger", type=float, default=6.0, help="seconds between lane starts")
    ap.add_argument("--shared-mic", action="store_true",
                    help="all lanes share the one virtmic (default: per-lane HARVEST_MIC_SUFFIX sink)")
    ap.add_argument("--no-camera-daemon", action="store_true",
                    help="do not start the shared camera feeder (assume one is already running)")
    ap.add_argument("--fresh-claims", action="store_true",
                    help="truncate the claim file first (re-partition the whole current pending set)")
    args = ap.parse_args()

    # Run from the repo root so config.py's .env + every module-relative data path resolve to THIS
    # checkout (harvest_state / assessment_bank / taleo_accounts), regardless of the launch cwd. Guards
    # the `-m backend…` foot-gun where the shell cwd's own (empty) backend package would shadow this one.
    os.chdir(REPO)
    os.makedirs(LOG_DIR, exist_ok=True)
    if args.fresh_claims or not os.path.exists(CLAIM_FILE):
        open(CLAIM_FILE, "w").close()

    pending = _discover_pending()
    _log(f"pending {PLATFORM}: {len(pending)} invite(s); lanes={args.lanes} "
         f"session={args.session_secs}s hard={args.hard_timeout}s mic="
         f"{'shared' if args.shared_mic else 'per-lane'}")
    if not pending:
        _log("nothing pending — exiting")
        return

    if not args.no_camera_daemon:
        _ensure_camera_daemon()

    threads = []
    for i in range(args.lanes):
        t = threading.Thread(target=_lane, args=(i, args.session_secs, args.hard_timeout,
                                                 args.shared_mic), name=f"lane{i}", daemon=True)
        t.start()
        threads.append(t)
        time.sleep(args.stagger)   # let each lane's mic/camera setup + pulse default settle
    for t in threads:
        t.join()
    _log(f"ALL LANES DONE — claimed={_counts['claimed']} completed={_counts['completed']} "
         f"other={_counts['other']} timeout={_counts['timeout']} error={_counts['error']}")


if __name__ == "__main__":
    main()
