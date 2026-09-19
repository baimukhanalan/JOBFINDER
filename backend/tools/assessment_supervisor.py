"""Self-terminating supervisor for the Sutherland→AMCAT (Mac) assessment lane.

The Mac's real camera is the only way past the AMCAT WCI200 proctor, reached over a Tailscale-nc →
socat CDP tunnel. This tool makes that lane SELF-MANAGING + OBSERVABLE (owner ask 2026-09-18):

  * AUTO-START — ONLY when there is FRESH work (a Sutherland invite not already done/skipped) — the WHOLE
    Mac drive rig: the CDP tunnel, the keep-awake (`caffeinate`), AND OBS + its Virtual Camera
    (`_ensure_obs`, the AMCAT WCI200 proctor needs the Mac's REAL camera, which the OBS Virtual Camera
    feeds over CDP). OBS start is idempotent — an already-running OBS is LEFT alone (never restarted, so a
    live drive's camera is never disturbed).
  * DRIVE each fresh invite through the Mac by FRESH-NAV (its own autologin link, `adapter.enter` hard-
    reloads first so the shared Mac tab can't re-read a stale parked page) — harvest_runner marks CRM
    «пройдено» on a real completion. Keeps the Mac awake WHILE driving via `MAC_SSH` (`caffeinate`,
    30-min bounded, self-releases) — set MAC_SSH (e.g. 'macalan'); no-op otherwise.
  * SKIP policy — a skip must mean the invite is genuinely dead, never an infra miss:
      - camera wall (Mac OBS not feeding) → NEVER skipped (temporary Mac issue, retry later);
      - Mac/CDP hiccup / partial hang (`transient`) → NEVER skipped (retry later);
      - `link-expired` (dead SHL token) → skipped in ONE pass (unrecoverable);
      - reached-but-empty (0 items, «evaluating»/submitted) → skipped after `_SKIP_AFTER` cumulative
        low-yield attempts (persisted `sutherland_attempts.json`).
  * AUTO-STOP: when no fresh work remains (or the Mac is offline), close OBS (`_stop_obs`, so the Mac can
    idle/sleep — GUARDED: never while a local `shl_sutherland` drive is still using the camera) and tear
    the tunnel DOWN, then EXIT. Never spins — the Health «Ассессменты» group + `health --alert` surface
    an offline Mac / futile churn. (pmset never-sleep is best-effort only: `sudo -n pmset` needs a
    password on the Mac, so keep-awake leans on the owner's pre-set `SleepDisabled=1` + `caffeinate`.)

ONE-SHOT + flock (single instance): invoke from cron (`*/15`, flock-skipped when a run overruns) and
event-driven from `mail_indexer` on a fresh talentcentral invite. `--dry-run` reports intent only;
`--max N` bounds how many invites one invocation drives (default 8).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAC_IP = os.environ.get("MAC_IP", "100.86.135.112")
_MAC_CDP_PORT = 9223           # Chrome remote-debug port ON the Mac
_PORT = 9222                   # local tunnel port
_LOCK = "/tmp/jf_assess_supervisor.lock"
_ATTEMPTS = os.path.join(_ROOT, "backend", "data", "sutherland_attempts.json")
_SKIP_AFTER = 3                # low-yield attempts before a stuck invite is skipped
# Sutherland's AMCAT battery is personality-heavy (AMPI/OPQ forced-choice, ~140-160 items at ~8s each);
# the harvester's 1200s (20-min) default session cap left a real battery UNFINISHED at ~141 items
# (`partial_timeout`). Only ONE invite is live at a time (the rest expire), so a longer per-drive budget
# is the right trade: `_SESSION_SECS` (the walk wall-clock) lets a ~180-item battery COMPLETE in one
# session; `_PER_JOB_TIMEOUT` (the subprocess cap) stays ABOVE it so the post-walk ASR banking finishes.
_SESSION_SECS = 1500           # HARVEST_SESSION_SECS for the walk (25 min)
_PER_JOB_TIMEOUT = 1800        # 30 min hard subprocess cap (must exceed _SESSION_SECS)
# A drive that reached NOTHING for an INFRA reason (CDP/Mac/proxy hiccup) — NOT a verdict on the invite,
# so it must never accrue toward the skip cap (a camera wall + a partial hang are handled separately).
_TRANSIENT_RE = re.compile(
    r"connect_over_cdp|targetclos|target closed|browser has been closed|websocket|disconnected|"
    r"connection refused|econnrefused|net::err|\berr_[a-z_]+|read timed out|"
    r"traceback \(most recent", re.I)


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
        # discover() yields the mailbox LOCAL-PART; the done/skip sets store FULL emails
        # (name@takhet.com). Compare on the full form — else the exclusion is a total no-op and the
        # lane re-drives already-done/skipped invites forever + never converges (never auto-stops).
        full = mbx if "@" in mbx else f"{mbx}@takhet.com"
        if full not in done and full not in skip:
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


def _egress_sock() -> str | None:
    """Pick a live userspace-egress `tailscaled` socket that can reach the Mac. The `*/10
    tailscale_egress --sync` cron CHURNS which slot is live, so slot 0 is NOT guaranteed to exist —
    the old hardcoded `ts-egress/0/tailscaled.sock` silently broke `_tunnel_up` whenever the cron had
    rebuilt the slots (only a pre-existing socat then kept the lane alive). Scan all slots and prefer
    one whose tailnet status actually sees the Mac's node; fall back to the first existing socket."""
    import glob as _glob
    socks = sorted(_glob.glob(os.path.join(_ROOT, "backend/data/ts-egress/*/tailscaled.sock")))
    fallback = None
    for s in socks:
        fallback = fallback or s
        try:
            r = subprocess.run(["tailscale", f"--socket={s}", "status"],
                               cwd=_ROOT, capture_output=True, text=True, timeout=8)
            out = (r.stdout or "").lower()
            if _MAC_IP in (r.stdout or "") or "macbook-air-alan" in out:
                return s
        except Exception:
            continue
    return fallback


def _tunnel_up() -> bool:
    """Bring the CDP tunnel to the Mac up if it isn't. False when no egress socket can reach the Mac."""
    if _tunnel_listening():
        return True
    sock = _egress_sock()
    if not sock:
        _log("no egress tailscaled socket found — cannot reach the Mac")
        return False
    subprocess.Popen(
        ["socat", f"TCP-LISTEN:{_PORT},bind=127.0.0.1,reuseaddr,fork,keepalive",
         f"EXEC:tailscale --socket={sock} nc {_MAC_IP} {_MAC_CDP_PORT}"],
        cwd=_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(2)
    return _tunnel_listening()


def _tunnel_down() -> None:
    subprocess.run(["pkill", "-f", f"socat TCP-LISTEN:{_PORT}.*{_MAC_IP} {_MAC_CDP_PORT}"],
                   capture_output=True)


def _mac_online() -> bool:
    # After a FRESH _tunnel_up(), the FIRST request through a cold `tailscale nc` egress hop can take
    # well over 6s to establish the peer path to the Mac — a single short probe then false-negatives
    # "Mac OFFLINE" and skips a perfectly reachable Mac (observed 2026-09-18: CDP :9222 answered
    # instantly seconds later). RETRY a few times so a cold tunnel warms up before we give up.
    for _ in range(5):
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{_PORT}/json/version", timeout=8)
            if b"Browser" in r.read():
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def _keep_mac_awake() -> str:
    """Prevent the Mac from idle-sleeping WHILE we drive tests (Chrome denies a CDP screen-wake-lock, so
    SSH `caffeinate` is the only automatable path). Best-effort + env-gated: set `MAC_SSH` to the Mac's
    login (e.g. 'alan@100.86.135.112') AFTER enabling Remote Login on the Mac; a no-op otherwise. Runs a
    bounded `caffeinate -dimsu -t 1800` so it self-releases after 30 min even if we die. Returns a status
    line for the log/Health. (Simplest alternative, no SSH: set the Mac to never-sleep in Energy/pmset.)"""
    target = os.environ.get("MAC_SSH", "").strip()
    if not target:
        return ("no-sleep NOT auto-managed — enable Remote Login + set MAC_SSH (e.g. an ssh-config host "
                "like 'macalan' whose ProxyCommand tunnels the egress slot), or set the Mac to "
                "never-sleep (pmset/Energy)")
    # MAC_SSH may be a single ssh alias ('macalan', which self-heals via its config ProxyCommand — no
    # long-lived socat tunnel to die) OR a full multi-token target ('-p 2222 alan@127.0.0.1'). shlex so
    # either works. Popen is fire-and-forget with start_new_session: ssh holds `caffeinate -dimsu -t
    # 1800` (30-min bounded — self-releases even if we crash), so the Mac stays awake only while a drive
    # window is open, then sleeps again (the owner's ask: awake WHILE driving, else sleep).
    try:
        ssh_target = shlex.split(target)
        # DETACH the remote caffeinate (nohup + closed fds + `&`) so it OUTLIVES this SSH channel.
        # A plain `ssh … "caffeinate …"` ties caffeinate to the SSH session — when the fire-and-forget
        # client exits or the flaky tailscale-nc ProxyCommand blips, the remote caffeinate gets SIGHUP
        # and dies, letting the Mac sleep mid-drive. nohup+disown keeps it holding the full 30-min window.
        subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                          "-o", "StrictHostKeyChecking=accept-new", *ssh_target,
                          "pkill caffeinate 2>/dev/null; nohup caffeinate -dimsu -t 1800 "
                          ">/dev/null 2>&1 </dev/null & disown 2>/dev/null; true"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return f"caffeinate started on the Mac via '{target}' (30-min window)"
    except Exception as e:
        return f"caffeinate SSH failed ({str(e)[:50]}) — check Remote Login / MAC_SSH"


def _mac_ssh(target: str, remote_cmd: str, timeout: int) -> "subprocess.CompletedProcess | None":
    """Run ONE blocking SSH command on the Mac (best-effort). `target` is MAC_SSH, shlex-split — an ssh
    alias like 'macalan' (its ProxyCommand self-heals the egress hop) OR a full '-p 2222 user@host'.
    Returns the CompletedProcess, or None if SSH itself failed/timed out — callers treat None as
    'Mac unreachable' and never raise."""
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
             "-o", "StrictHostKeyChecking=accept-new", *shlex.split(target), remote_cmd],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _obs_running(target: str) -> "bool | None":
    """True/False if OBS is up on the Mac; None if the Mac was unreachable over SSH."""
    r = _mac_ssh(target, "pgrep -x OBS >/dev/null && echo up || echo down", 12)
    if r is None:
        return None
    return "up" in (r.stdout or "")


def _obs_camera_present(target: str) -> bool:
    """Best-effort: is an OBS Virtual Camera device listed on the Mac? `system_profiler SPCameraDataType`
    is slow (several s) so this is ONE generously-timed, NON-fatal probe — a False means 'presence
    unconfirmed', NOT 'camera down' (we never restart a running OBS on the strength of it — see
    `_ensure_obs`)."""
    r = _mac_ssh(target, "system_profiler SPCameraDataType 2>/dev/null", 25)
    return bool(r) and ("obs virtual" in (r.stdout or "").lower())


def _ensure_obs() -> str:
    """Bring OBS + its Virtual Camera UP on the Mac BEFORE a drive — the AMCAT WCI200 proctor needs the
    Mac's REAL camera, which the OBS Virtual Camera feeds over CDP. Best-effort + env-gated on MAC_SSH,
    never raises (like `_keep_mac_awake`). If OBS is ALREADY running, LEAVE it (idempotent no-op — assume
    its cam is feeding, so a live drive's camera is never disturbed by a restart; a running-OBS-with-cam-
    off is a logged note, not a forced restart). Else `open -a OBS --args --startvirtualcam
    --minimize-to-tray` (headless, no sudo) + poll (≤~20s) until the OBS process is up, then a one-shot
    best-effort virtual-cam presence note. Returns a status line for the log/Health."""
    target = os.environ.get("MAC_SSH", "").strip()
    if not target:
        return ("OBS not auto-managed — set MAC_SSH (e.g. 'macalan') + keep OBS's Virtual Camera on, "
                "or start OBS manually on the Mac")
    up = _obs_running(target)
    if up is None:
        return "OBS state unknown — Mac unreachable over SSH (check Remote Login / MAC_SSH)"
    if up:
        return "OBS already running — leaving its Virtual Camera as-is (no restart)"
    # not up → start it HEADLESSLY with the virtual cam already feeding (no sudo; won't steal focus)
    _mac_ssh(target, "open -a OBS --args --startvirtualcam --minimize-to-tray", 15)
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(2)
        if _obs_running(target):
            cam = _obs_camera_present(target)
            return ("OBS started (--startvirtualcam)"
                    + (" + Virtual Camera detected" if cam else " (virtual-cam presence unconfirmed)"))
    return "OBS start issued but process not confirmed up within ~20s (Mac may be offline)"


def _drive_running_locally() -> bool:
    """True if a `shl_sutherland` `harvest_runner` drive is executing on THIS server (its Chrome-over-CDP
    session is USING the Mac's OBS camera). Guards `_stop_obs` so a manual/concurrent drive's proctored
    camera is never killed out from under it. The supervisor's OWN cmdline doesn't contain 'harvest_runner'
    so it never self-matches."""
    try:
        r = subprocess.run(["pgrep", "-f", "harvest_runner.*shl_sutherland"],
                           capture_output=True, text=True, timeout=5)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def _stop_obs() -> str:
    """Close OBS on the Mac (its Virtual Camera stops) so the Mac can idle/sleep when NO test is pending.
    Called ONLY in the AUTO-STOP path (idle / queue drained / exit) where `_fresh()` is empty, so no invite
    remains to drive. Best-effort + env-gated on MAC_SSH, never raises. GUARDED: refuses to kill OBS while a
    `shl_sutherland` drive is running on THIS server (its CDP session is using the live proctored camera —
    a kill would break it). Also BEST-EFFORT releases the manual never-sleep (`sudo -n pmset disablesleep
    0`), which needs a password on this Mac → silently ignored (the pre-set `SleepDisabled=1` + caffeinate
    already cover keep-awake; we do NOT block on pmset)."""
    target = os.environ.get("MAC_SSH", "").strip()
    if not target:
        return "OBS not auto-managed (no MAC_SSH)"
    if _drive_running_locally():
        return "OBS left UP — a Sutherland drive is still running (camera in use)"
    r = _mac_ssh(target,
                 "pkill -x OBS 2>/dev/null; sudo -n pmset disablesleep 0 2>/dev/null; true", 20)
    return "OBS stopped on the Mac (idle)" if r is not None else "OBS stop skipped — Mac unreachable"


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


def _drive(mbx: str, url: str) -> tuple[bool, int, bool, bool, bool]:
    """Run one harvest_runner drive on the Mac's Chrome (CDP).
    Returns (completed, items, camera_wall, transient, expired).

    FRESH-NAV (no HARVEST_CDP_RESUME): the supervisor drains a QUEUE of DIFFERENT invites through the ONE
    reused Mac tab, so each drive MUST navigate to ITS OWN invite link. RESUME=1 skipped the nav whenever
    the tab was already on an assessment URL, so every invite re-read whatever single page happened to be
    parked there (typically a prior invite's `/evaluating` or `link-expired`) → a MASS false «low-yield»
    that skipped genuinely-live invites. `adapter.enter` now hard-reloads (about:blank first) so the fresh
    autologin token is processed even when the tab is parked on a same-origin talentcentral route."""
    env = {**os.environ, "HARVEST_CDP_URL": f"http://127.0.0.1:{_PORT}",
           "HARVEST_CDP_TAB_INDEX": "0", "DISPLAY": ":98", "PYTHONPATH": ".", "HARVEST_FAST": "1",
           "HARVEST_SESSION_SECS": str(_SESSION_SECS)}
    env.pop("HARVEST_CDP_RESUME", None)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "backend.tools.harvest_runner", "--platform", "shl_sutherland",
             "--url", url, "--mailbox", mbx],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=_PER_JOB_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        # a hung drive is a Mac/CDP hiccup, NOT a property of the invite → transient, never a skip signal
        return False, 0, False, True, False
    low = out.lower()
    completed = ("status : completed" in low) or ("marked assessment done" in low)
    items = len([ln for ln in out.splitlines() if " #" in ln and " q=" in ln])
    # camera wall (Mac OBS momentarily not feeding / real webcam not detected) — NEVER a skip signal
    camera = ("proctor_camera" in low) or ("wci200" in low) or ("unable to detect a camera" in low)
    # the emailed SHL autologin token is DEAD (SPA redirected to `#/link-expired`) — a terminal state
    expired = ("link-expired" in low) or ("link expired" in low)
    m = re.search(r"status\s*:\s*(\S+)", low)
    status = m.group(1) if m else ""
    # TRANSIENT = reached nothing for an INFRA reason (CDP/Mac/proxy hiccup or an EARLY hang) — must NOT
    # accrue toward the skip cap. A `partial_timeout` that already banked items (>=3) is NOT an infra
    # miss — it's a long-battery partial (the AMPI battery outlasts the harvester's session cap; the
    # assessment RESUMES server-side next run) → let it fall to the `partial` branch so it's logged
    # accurately as progress, not a "hiccup". Both paths retry; neither skips.
    transient = ((status == "error") or bool(_TRANSIENT_RE.search(out))
                 or (status == "partial_timeout" and items < 3))
    return completed, items, camera, transient, expired


def run(dry: bool, max_jobs: int) -> None:
    fresh = _fresh()
    _log(f"fresh Sutherland invites: {len(fresh)}")
    if not fresh:
        if not dry:
            _log("OBS: " + _stop_obs())   # queue empty → let the Mac idle/sleep (guarded if a drive runs)
            _tunnel_down()
        _log("idle: no fresh work → OBS + tunnel DOWN, exit")
        return
    if dry:
        _log(f"[dry-run] would ensure OBS (Virtual Camera) + tunnel + caffeinate, then drive up to "
             f"{max_jobs} (mac_online check skipped)")
        return
    if not _tunnel_up():
        _log("no tunnel to the Mac — exit")
        return
    if not _mac_online():
        _log("Mac OFFLINE/asleep — not driving (Health shows «down» + alerts). Tunnel left up briefly.")
        return
    _log("Mac no-sleep: " + _keep_mac_awake())    # keep the Mac awake for the duration of the drives
    _log("OBS: " + _ensure_obs())                 # bring the Virtual Camera up before driving (WCI200)
    attempts = _load_attempts()
    passed = skipped = partial = 0
    for mbx, url in fresh[:max_jobs]:
        completed, items, camera, transient, expired = _drive(mbx, url)
        if completed:
            passed += 1
            attempts.pop(mbx, None)
            # harvest_runner's --url path only marks the CRM «пройдено» when --mailbox has an '@'
            # (we pass the local-part), and core/adapters never mark done — so the supervisor MUST
            # record the completion itself, else a real Mac-lane pass stays in «Действие» + is re-driven.
            try:
                from backend.tools import mailcrm
                mailcrm.mark_assessment_done(mbx)   # normalizes local-part → name@takhet.com
            except Exception:
                pass
            _log(f"PASSED {mbx} — marked «пройдено»")
        elif camera:
            # Mac OBS momentarily not feeding — a TEMPORARY Mac issue, NEVER counts toward a skip.
            _log(f"camera-wall {mbx} — Mac OBS/webcam not detected, NOT skipping (retry later)")
        elif expired:
            # the SHL autologin token is dead (link-expired) — a real, unrecoverable terminal state, so
            # retire it in ONE pass (no value re-driving a dead link every run; not a camera/infra miss).
            # CHECKED BEFORE `transient`: a link-expired drive can ALSO end status=error / print a
            # traceback (which trips the broad _TRANSIENT_RE), and the old order labelled such a dead
            # token "transient" forever — never retiring it. link-expired is terminal; it wins.
            from backend.tools import mailcrm
            mailcrm.mark_assessment_skipped(mbx)
            attempts.pop(mbx, None)
            skipped += 1
            _log(f"SKIP {mbx} (link-expired — dead SHL token, unrecoverable)")
        elif transient:
            # a CDP/Mac/proxy hiccup or a partial hang — retry, do NOT accrue toward the skip cap.
            _log(f"transient {mbx} — Mac/CDP hiccup, NOT skipping (retry later)")
        elif items < 3:
            # reached the assessment but nothing to answer — genuinely stuck (submitted/«evaluating»).
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
            # made real progress (banked items) but didn't finish — a long battery that outran the
            # session cap RESUMES server-side next run; clear any accrued low-yield attempts.
            partial += 1
            attempts.pop(mbx, None)
            _log(f"partial {mbx} ({items} items) — retry/resume next run")
        _save_attempts(attempts)
    if not _fresh():
        _log("OBS: " + _stop_obs())   # nothing left to drive → close OBS so the Mac can idle/sleep
        _tunnel_down()
        _log("queue drained → OBS + tunnel DOWN")
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
