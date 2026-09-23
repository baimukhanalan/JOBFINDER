"""Self-verifying probe → auto-arm for the Randstad résumé-drop lane (FriendlyCaptcha, 2captcha).

The Randstad drop lane is wired (fill + FriendlyCaptcha solve via 2captcha) but must not go live on
faith: a real drop has to clear FriendlyCaptcha AND land the on-page thank-you. This probe verifies
that WITHOUT a human babysitting it, mirroring `workday_probe_promote`:

  * Runs ONLY on a genuinely quiet box (load1 < QUIET_LOAD AND no jobfinder headful drive owns :98),
    else exits 0 immediately (the next tick retries) — it never adds to a load spike.
  * When quiet it drives ONE real drop (`randstad_recon`, RANDSTAD_ADVANCE=1). On a real ack it writes
    the gitignored ARMING MARKER `data/randstad_verified.json` (`{"randstad": {"verified": true, …}}`),
    which a live cron checks before dropping for real — so promotion is code-free.
  * A drive that fills + reaches submit but does NOT get the on-page thank-you (FriendlyCaptcha not
    cleared / no 2captcha key / a validation stall) is left PENDING with the honest reason recorded to
    `data/randstad_probe_blocked.json` — never armed on partial evidence.

Idempotent + fcntl-locked. `--dry-run` reports the quiet gate + whether a 2captcha key is present,
driving nothing. Manual: `python -m backend.tools.randstad_probe_promote [--force] [--dry-run]`.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import randstad_recon as rr  # noqa: E402

LANE = "randstad"
QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))
DRIVE_SECS = int(os.getenv("PROBE_DRIVE_SECS", "600"))
LOCK_PATH = os.path.join(REPO, "logs", "randstad_probe_promote.lock")
VERIFIED_PATH = os.path.join(REPO, "data", "randstad_verified.json")
BLOCKED_PATH = os.path.join(REPO, "data", "randstad_probe_blocked.json")
LOG = print


def _load1() -> float:
    try:
        return os.getloadavg()[0]
    except Exception:
        return 999.0


def _jobfinder_headful_drives() -> int:
    """Count LIVE jobfinder headful recon drives (they own :98) — the probe must not fight them."""
    try:
        out = subprocess.run(["ps", "-eo", "cmd"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 99
    pat = re.compile(r"python3 -m backend\.tools\.(workday_recon|foundever_recon|smartrecruiters_recon|"
                     r"gainwell_recon|icims_recon|randstad_recon|shl_assess_runner) .*--(job|drain)\b")
    n = sum(1 for ln in out.splitlines() if pat.search(ln))
    # randstad_recon has no --job/--drain flag; count any live randstad_recon drive explicitly too.
    n += sum(1 for ln in out.splitlines()
             if "backend.tools.randstad_recon" in ln and "--list" not in ln
             and "probe_promote" not in ln)
    return n


def box_is_quiet() -> tuple[bool, str]:
    load = _load1()
    drives = _jobfinder_headful_drives()
    ok = load < QUIET_LOAD and drives <= int(os.getenv("PROBE_MAX_DRIVES", "0"))
    return ok, f"load1={load:.1f}(<{QUIET_LOAD}) jobfinder_drives={drives}"


def is_verified() -> bool:
    try:
        with open(VERIFIED_PATH) as f:
            v = json.load(f) or {}
        return bool(v.get(LANE, {}).get("verified"))
    except Exception:
        return False


def _twocaptcha_key_present() -> bool:
    rr.load_env()
    return bool(os.getenv("TWOCAPTCHA_KEY") or os.getenv("CAPTCHA_SOLVER_KEY"))


def mark_verified() -> None:
    cur = {}
    try:
        if os.path.exists(VERIFIED_PATH):
            with open(VERIFIED_PATH) as f:
                cur = json.load(f) or {}
    except Exception:
        cur = {}
    cur[LANE] = {"verified": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "via": "friendlycaptcha_2captcha"}
    _atomic_write(VERIFIED_PATH, cur)


def _mark_blocked(reason: str) -> None:
    cur = {}
    try:
        if os.path.exists(BLOCKED_PATH):
            with open(BLOCKED_PATH) as f:
                cur = json.load(f) or {}
    except Exception:
        cur = {}
    cur[LANE] = {"reason": reason, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _atomic_write(BLOCKED_PATH, cur)


def _atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _drive() -> str:
    """Drive ONE real drop; return the raw log text (never raises)."""
    # Headless: FriendlyCaptcha is cleared by a 2captcha token injection (no visible challenge), so the
    # probe needs no :98 — keeps it off the shared display even on a quiet box.
    env = dict(os.environ, RANDSTAD_ADVANCE="1", RANDSTAD_HEADLESS="1")
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.randstad_recon --keep 6"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, submit_no_ack, no_submit, no_key, error}."""
    if "no 2captcha key" in log:
        return "no_key", "RANDSTAD_ADVANCE set but no 2captcha key — FriendlyCaptcha uncleared"
    if re.search(r"success=True", log) or "mail_seen" in log:
        return "confirmed", "on-page Randstad thank-you (drop accepted)"
    if re.search(r"submitted=True", log):
        return "submit_no_ack", "reached submit but no on-page ack (FriendlyCaptcha/validation stall)"
    if re.search(r"submitted=False", log) or "dry run" in log:
        return "no_submit", "fill did not reach a real submit"
    return "error", "drive did not produce a submit verdict"


def run_once(force: bool = False, dry: bool = False) -> dict:
    if is_verified():
        LOG("randstad_probe_promote: already verified/armed — nothing to do")
        return {"done": True}
    quiet, why = box_is_quiet()
    key = _twocaptcha_key_present()
    if dry:
        LOG(f"randstad_probe_promote DRY: quiet={quiet} ({why}); 2captcha_key_present={key}; "
            f"would drive one real drop." + ("" if key else " [NO KEY — would report no_key]"))
        return {"dry": True, "quiet": quiet, "key_present": key}
    if not (quiet or force):
        LOG(f"randstad_probe_promote: box busy ({why}) — skip, retry next tick.")
        return {"skipped": True, "why": why}
    if not key and not force:
        _mark_blocked("no 2captcha key configured (TWOCAPTCHA_KEY/CAPTCHA_SOLVER_KEY)")
        LOG("randstad_probe_promote: no 2captcha key — FriendlyCaptcha can't be solved; leaving pending.")
        return {"skipped": True, "why": "no_key"}
    LOG(f"randstad_probe_promote: driving one real drop ({why})")
    log = _drive()
    verdict, detail = classify(log)
    LOG(f"randstad_probe_promote: {LANE} -> {verdict} ({detail})")
    if verdict == "confirmed":
        mark_verified()
        LOG(f"randstad_probe_promote: ARMED {LANE} -> data/randstad_verified.json")
    else:
        _mark_blocked(detail)
    return {"lane": LANE, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet / no key")
    ap.add_argument("--dry-run", action="store_true", help="report the quiet gate + key presence, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("randstad_probe_promote: another run holds the lock — exiting")
        return
    try:
        run_once(force=args.force, dry=args.dry_run)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    main()
