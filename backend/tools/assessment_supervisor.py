"""Self-terminating supervisor for the Sutherland→AMCAT (Mac) assessment lane.

The Mac's real camera is the only way past the AMCAT WCI200 proctor, reached over a Tailscale-nc →
socat CDP tunnel. This tool makes that lane SELF-MANAGING + OBSERVABLE (owner ask 2026-09-18):

  * AUTO-START the tunnel ONLY when there is FRESH work (a Sutherland invite not already done/skipped).
  * DRIVE the fresh invites through the Mac (harvest_runner marks CRM «пройдено» on a real completion).
  * A genuinely STUCK invite (0 items) — almost always one already in the SHL «evaluating» (submitted)
    state, so there is nothing left to answer — is marked SKIPPED, but ONLY after `_SKIP_AFTER`
    cumulative low-yield attempts (persisted), so a transient miss never skips a real one. A
    proctor-camera wall (Mac OBS down) is NEVER skipped — it's a temporary Mac issue, retry later.
  * AUTO-STOP: when no fresh work remains (or the Mac is offline), tear the tunnel DOWN and EXIT. Never
    spins — the Health «Ассессменты» group + `health --alert` surface an offline Mac / futile churn.

ONE-SHOT + flock (single instance): invoke from cron (`*/15`, flock-skipped when a run overruns) and
event-driven from `mail_indexer` on a fresh talentcentral invite. `--dry-run` reports intent only;
`--max N` bounds how many invites one invocation drives (default 8).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAC_IP = os.environ.get("MAC_IP", "100.86.135.112")
_MAC_CDP_PORT = 9223           # Chrome remote-debug port ON the Mac
_PORT = 9222                   # local tunnel port
_SLOT = 0
_SOCK = f"backend/data/ts-egress/{_SLOT}/tailscaled.sock"
_LOCK = "/tmp/jf_assess_supervisor.lock"
_ATTEMPTS = os.path.join(_ROOT, "backend", "data", "sutherland_attempts.json")
_SKIP_AFTER = 3                # low-yield attempts before a stuck invite is skipped
_PER_JOB_TIMEOUT = 1500        # 25 min hard cap per drive


def _log(msg: str) -> None:
    print(f"[assess-sup {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _fresh() -> list[tuple[str, str]]:
    """Sutherland invites NOT already CRM-done or skipped (include_done=True: the link re-mints)."""
    from backend.tools import mailcrm
    from backend.tools.assessment_harvester import discover
    done = set(mailcrm.assessment_done_mailboxes())
    skip = set(mailcrm.assessment_skipped_mailboxes())
    out = []
    for mbx, url in discover.discover("shl_sutherland", limit=800, include_done=True):
        if mbx not in done and mbx not in skip:
            out.append((mbx, url))
    return out


def _tunnel_listening() -> bool:
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", _PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _tunnel_up() -> bool:
    """Bring the CDP tunnel to the Mac up if it isn't. False when the egress socket is missing."""
    if _tunnel_listening():
        return True
    if not os.path.exists(os.path.join(_ROOT, _SOCK)):
        _log(f"egress socket {_SOCK} missing — cannot reach the Mac")
        return False
    subprocess.Popen(
        ["socat", f"TCP-LISTEN:{_PORT},bind=127.0.0.1,reuseaddr,fork",
         f"EXEC:tailscale --socket={_SOCK} nc {_MAC_IP} {_MAC_CDP_PORT}"],
        cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(2)
    return _tunnel_listening()


def _tunnel_down() -> None:
    subprocess.run(["pkill", "-f", f"socat TCP-LISTEN:{_PORT}.*{_MAC_IP} {_MAC_CDP_PORT}"],
                   capture_output=True)


def _mac_online() -> bool:
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/json/version", timeout=6)
        return b"Browser" in r.read()
    except Exception:
        return False


def _keep_mac_awake() -> str:
    """Prevent the Mac from idle-sleeping WHILE we drive tests (Chrome denies a CDP screen-wake-lock, so
    SSH `caffeinate` is the only automatable path). Best-effort + env-gated: set `MAC_SSH` to the Mac's
    login (e.g. 'alan@100.86.135.112') AFTER enabling Remote Login on the Mac; a no-op otherwise. Runs a
    bounded `caffeinate -dimsu -t 1800` so it self-releases after 30 min even if we die. Returns a status
    line for the log/Health. (Simplest alternative, no SSH: set the Mac to never-sleep in Energy/pmset.)"""
    target = os.environ.get("MAC_SSH", "").strip()
    if not target:
        return "no-sleep NOT auto-managed — enable Remote Login + set MAC_SSH, or set the Mac to never-sleep (pmset/Energy)"
    try:
        subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                          "-o", "StrictHostKeyChecking=accept-new", target, "caffeinate -dimsu -t 1800"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return f"caffeinate started on the Mac via {target} (30-min window)"
    except Exception as e:
        return f"caffeinate SSH failed ({str(e)[:50]}) — check Remote Login / MAC_SSH"


def _load_attempts() -> dict:
    try:
        with open(_ATTEMPTS) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_attempts(d: dict) -> None:
    try:
        tmp = f"{_ATTEMPTS}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _ATTEMPTS)
    except Exception:
        pass


def _drive(mbx: str, url: str) -> tuple[bool, int, bool]:
    """Run one harvest_runner drive on the Mac. Returns (completed, items, camera_wall)."""
    env = {**os.environ, "HARVEST_CDP_URL": f"http://127.0.0.1:{_PORT}", "HARVEST_CDP_RESUME": "1",
           "HARVEST_CDP_TAB_INDEX": "0", "DISPLAY": ":98", "PYTHONPATH": ".", "HARVEST_FAST": "1"}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "backend.tools.harvest_runner", "--platform", "shl_sutherland",
             "--url", url, "--mailbox", mbx],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=_PER_JOB_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, 0, False
    completed = ("status : completed" in out) or ("marked assessment done" in out)
    items = len([ln for ln in out.splitlines() if " #" in ln and " q=" in ln])
    camera = "proctor_camera" in out.lower()
    return completed, items, camera


def run(dry: bool, max_jobs: int) -> None:
    fresh = _fresh()
    _log(f"fresh Sutherland invites: {len(fresh)}")
    if not fresh:
        if not dry:
            _tunnel_down()
        _log("idle: no fresh work → tunnel DOWN, exit")
        return
    if dry:
        _log(f"[dry-run] would ensure tunnel + drive up to {max_jobs} (mac_online check skipped)")
        return
    if not _tunnel_up():
        _log("no tunnel to the Mac — exit")
        return
    if not _mac_online():
        _log("Mac OFFLINE/asleep — not driving (Health shows «down» + alerts). Tunnel left up briefly.")
        return
    _log("Mac no-sleep: " + _keep_mac_awake())    # keep the Mac awake for the duration of the drives
    attempts = _load_attempts()
    passed = skipped = partial = 0
    for mbx, url in fresh[:max_jobs]:
        completed, items, camera = _drive(mbx, url)
        if completed:
            passed += 1
            attempts.pop(mbx, None)
            _log(f"PASSED {mbx}")
        elif camera:
            _log(f"camera-wall {mbx} — Mac OBS down, NOT skipping (retry later)")
        elif items < 3:
            attempts[mbx] = int(attempts.get(mbx, 0)) + 1
            if attempts[mbx] >= _SKIP_AFTER:
                from backend.tools import mailcrm
                mailcrm.mark_assessment_skipped(mbx)
                attempts.pop(mbx, None)
                skipped += 1
                _log(f"SKIP {mbx} ({_SKIP_AFTER} low-yield attempts — stuck/«evaluating»)")
            else:
                _log(f"low-yield {mbx} (attempt {attempts[mbx]}/{_SKIP_AFTER})")
        else:
            partial += 1
            attempts.pop(mbx, None)
            _log(f"partial {mbx} ({items} items) — retry next run")
        _save_attempts(attempts)
    if not _fresh():
        _tunnel_down()
        _log("queue drained → tunnel DOWN")
    _log(f"done: passed={passed} skipped={skipped} partial={partial}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Self-terminating Sutherland/Mac assessment supervisor.")
    ap.add_argument("--dry-run", action="store_true", help="report intent only, drive nothing")
    ap.add_argument("--max", type=int, default=8, help="max invites to drive this invocation")
    args = ap.parse_args()
    lock = open(_LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        _log("another supervisor is running — exit")
        return
    try:
        run(args.dry_run, args.max)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    main()
