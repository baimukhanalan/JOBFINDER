"""Self-verifying probe + auto-promoter for the Afni (ADP "myjobs") email-OTP apply lane.

The Afni lane (`afni_recon` + `strategies/adp.AdpStrategy`) bootstraps a candidate account with an
EMAILED OTP and submits the application. Before it is trusted as a LIVE auto-apply lane it must be
proven to reach a real ack — the create-profile + OTP + submit walk false-fails when the shared :98
is under an external CPU-load spike (a headful drive hangs mid-wizard). This cron proves it WITHOUT
a human babysitting it, mirroring `workday_probe_promote`:

  * It runs ONLY when the box is genuinely quiet (load1 < QUIET_LOAD AND no jobfinder headful drive),
    else it exits 0 immediately (the next tick retries) — so it never adds to a load spike.
  * When quiet it drives ONE live Afni job to a real ack (afni_recon, AFNI_ADVANCE=1). On a confirmed
    application (the Afni/ADP receipt in the persona Maildir, or the on-page confirmation) it appends
    'afni' to the gitignored data/afni_verified.json marker — the signal that the lane is proven and a
    live cron may drive it. NO code edit.
  * A drive that reaches the OTP account step but does NOT complete the submit is left UNVERIFIED
    (logged) — never promoted on partial evidence. A drive that hits an unexpected hard wall (e.g. a
    captcha appears, or a phone-SMS step the Maildir can't receive) is recorded to
    data/afni_probe_blocked.json with the honest reason.

Idempotent + fcntl-locked. `--dry-run` reports the quiet-gate decision + whether the lane is already
verified, driving nothing. Manual: `python -m backend.tools.afni_probe_promote [--job <id>] [--force]`.
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

from backend.tools import afni_recon  # noqa: E402

QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))    # 1-min load must be below this
DRIVE_SECS = int(os.getenv("PROBE_DRIVE_SECS", "600"))     # hard cap per probe drive
LOCK_PATH = os.path.join(REPO, "logs", "afni_probe_promote.lock")
VERIFIED_PATH = os.path.join(REPO, "data", "afni_verified.json")     # gitignored marker
BLOCKED_PATH = os.path.join(REPO, "data", "afni_probe_blocked.json")  # gitignored
LANE = "afni"
LOG = print


def _load1() -> float:
    try:
        return os.getloadavg()[0]
    except Exception:
        return 999.0


def _jobfinder_headful_drives() -> int:
    """Count LIVE jobfinder headful recon drives (they own :98) — a probe must not fight them."""
    try:
        out = subprocess.run(["ps", "-eo", "cmd"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 99
    pat = re.compile(r"python3 -m backend\.tools\.(workday_recon|foundever_recon|"
                     r"smartrecruiters_recon|gainwell_recon|icims_recon|amazon_recon|afni_recon|"
                     r"shl_assess_runner) .*--(job|drain)\b")
    return sum(1 for ln in out.splitlines() if pat.search(ln))


def box_is_quiet() -> tuple[bool, str]:
    load = _load1()
    drives = _jobfinder_headful_drives()
    ok = load < QUIET_LOAD and drives <= int(os.getenv("PROBE_MAX_DRIVES", "0"))
    return ok, f"load1={load:.1f}(<{QUIET_LOAD}) jobfinder_drives={drives}"


def _read_json_set(path: str) -> set:
    try:
        with open(path) as f:
            v = json.load(f)
        return set(v) if isinstance(v, list) else set(v.keys()) if isinstance(v, dict) else set()
    except Exception:
        return set()


def is_verified() -> bool:
    return LANE in _read_json_set(VERIFIED_PATH)


def add_verified(lane: str = LANE) -> None:
    """Append a lane name to the gitignored verified marker (atomic, idempotent)."""
    cur = _read_json_set(VERIFIED_PATH)
    cur.add(lane)
    os.makedirs(os.path.dirname(VERIFIED_PATH), exist_ok=True)
    tmp = VERIFIED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(cur), f)
    os.replace(tmp, VERIFIED_PATH)


def _mark_blocked(reason: str, lane: str = LANE) -> None:
    try:
        cur = {}
        if os.path.exists(BLOCKED_PATH):
            with open(BLOCKED_PATH) as f:
                cur = json.load(f) or {}
    except Exception:
        cur = {}
    cur[lane] = reason
    os.makedirs(os.path.dirname(BLOCKED_PATH), exist_ok=True)
    tmp = BLOCKED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f)
    os.replace(tmp, BLOCKED_PATH)


def next_job() -> int | None:
    """A live Afni mass_hiring_jobs id to probe (the create-account + OTP flow is job-uniform)."""
    try:
        ids = afni_recon.afni_job_ids()
    except Exception:
        return None
    return ids[0] if ids else None


def _drive(job: int) -> str:
    """Drive one afni_recon create-account→OTP→submit; return the raw log text (never raises)."""
    env = dict(os.environ, DISPLAY=os.getenv("DISPLAY", ":98"), AFNI_ADVANCE="1")
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.afni_recon --job {job} --keep 8"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, reached_account_incomplete, no_account, blocked, error}.

    PURE (network-free): reads the driver's own log lines.
      * confirmed                → the Maildir/on-page ack fired.
      * reached_account_incomplete → passed the OTP account but the submit did not confirm.
      * no_account               → never got past the auth/account-create step (OTP lagged / a
                                    create-profile field changed) — retry a future quiet tick.
      * blocked                  → an unexpected hard wall (a captcha appeared, or a phone-SMS step
                                    the Maildir can't receive) — recorded, not retried blindly.
      * error                    → the drive crashed / timed out.
    """
    low = log.lower()
    if "application confirmed" in low or "receipt in the maildir" in low:
        return "confirmed", "Afni/ADP receipt in the Maildir"
    if re.search(r"phone.?sms|sms code|text message code|verify.*phone number", low):
        return "blocked", "an SMS/phone-verification step the Maildir cannot receive"
    if re.search(r"captcha|recaptcha|hcaptcha|turnstile|aws.?waf", low):
        return "blocked", "an unexpected captcha wall appeared"
    if "submit clicked" in low and "no confirmation within" in low:
        return "reached_account_incomplete", "submitted but no ack detected within --keep"
    if "wizard reached submit" in low or "wizard_at_submit=true" in low:
        return "reached_account_incomplete", "passed OTP + reached Submit but did not complete"
    if "needs_account=true" in low or "reached the adp auth" in low or "ceiling: gated on" in low:
        return "no_account", "did not pass the ADP OTP account step (OTP lag / create-profile field)"
    if "run error" in low or "drive-error" in low or log.strip() == "TIMEOUT":
        return "error", "drive crashed or timed out"
    return "error", "drive did not reach a known checkpoint"


def run_once(force_job: int | None = None, force: bool = False, dry: bool = False) -> dict:
    if is_verified():
        LOG("afni_probe_promote: lane already verified (data/afni_verified.json) — nothing to do")
        return {"done": True, "verified": True}
    quiet, why = box_is_quiet()
    job = force_job or next_job()
    if job is None:
        LOG("afni_probe_promote: no active Afni job to probe")
        return {"done": True, "no_job": True}
    if not (quiet or force):
        LOG(f"afni_probe_promote: box busy ({why}) — skip, retry next tick. next job={job}")
        return {"skipped": True, "why": why, "next": job}
    if dry:
        LOG(f"afni_probe_promote DRY: would probe job {job}; {why}; verified={is_verified()}")
        return {"dry": True, "job": job, "why": why}
    LOG(f"afni_probe_promote: probing job={job} ({why})")
    log = _drive(job)
    verdict, detail = classify(log)
    LOG(f"afni_probe_promote: job {job} -> {verdict} ({detail})")
    if verdict == "confirmed":
        add_verified()
        LOG("afni_probe_promote: PROMOTED afni -> verified (data/afni_verified.json)")
    elif verdict == "blocked":
        _mark_blocked(detail)
    # reached_account_incomplete / no_account / error → leave pending, retry a future quiet tick
    return {"job": job, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job", type=int, default=None, help="probe one specific Afni job id")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the quiet gate + verified state, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("afni_probe_promote: another run holds the lock — exiting")
        return
    try:
        run_once(force_job=args.job, force=args.force, dry=args.dry_run)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    main()
