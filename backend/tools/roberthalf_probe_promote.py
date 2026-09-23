"""Self-verifying probe + auto-promoter for the account-walled staffing/insurer lanes
(Robert Half / Adecco / Progressive), mirroring workday_probe_promote.

The account-create → OTP → reCAPTCHA → submit verification of these collect-first tenants needs
a QUIET :98 (a single headful drive false-fails under an external CPU load spike). This driver:

  * runs ONLY when the box is genuinely quiet (load1 < QUIET_LOAD AND no jobfinder headful drive),
    else exits 0 immediately (the next tick retries) — never adds to a load spike;
  * when quiet, drives ONE active job to a REAL Maildir ack (the recon driver, <TENANT>_ADVANCE=1);
  * on a confirmed ack it writes the gitignored `data/<tenant>_verified.json` promotion marker
    (an HONEST "this lane reached a real ack" signal — these tenants are collect-first, so there
    is no live apply-cron to union it into; the marker records the verified verdict);
  * a run that reached the account wall + created the account but did NOT confirm is left PENDING
    (retry a future quiet tick); a run whose reCAPTCHA/account step BLOCKED is recorded to
    `data/<tenant>_probe_blocked.json` with the honest reason (a solver key + US IP may be needed).

Idempotent + fcntl-locked. `--dry-run` reports the quiet gate + the job it would drive.
Manual: `python -m backend.tools.roberthalf_probe_promote [--job <id>] [--force]`.
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

from backend.tools import roberthalf_recon as rr  # noqa: E402

QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))
DRIVE_SECS = int(os.getenv("PROBE_DRIVE_SECS", "600"))
DATA = os.path.join(REPO, "data")
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
                     r"smartrecruiters_recon|gainwell_recon|icims_recon|amazon_recon|"
                     r"roberthalf_recon|adecco_recon|progressive_recon|shl_assess_runner) "
                     r".*--(job|drain)\b")
    return sum(1 for ln in out.splitlines() if pat.search(ln))


def box_is_quiet() -> tuple[bool, str]:
    load = _load1()
    drives = _jobfinder_headful_drives()
    ok = load < QUIET_LOAD and drives <= 0
    return ok, f"load1={load:.1f}(<{QUIET_LOAD}) jobfinder_drives={drives}"


def _verified_path(source: str) -> str:
    return os.path.join(DATA, f"{source}_verified.json")


def _blocked_path(source: str) -> str:
    return os.path.join(DATA, f"{source}_probe_blocked.json")


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _mark(path: str, key: str, reason: str) -> None:
    cur = _read_json(path)
    cur[key] = {"reason": reason, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_json(path, cur)


def already_verified(source: str) -> bool:
    return bool(_read_json(_verified_path(source)))


def _drive(source: str, advance_env: str, job: int) -> str:
    """Drive one create-account→OTP→submit via the recon module; return the raw log (never raises)."""
    env = dict(os.environ, DISPLAY=os.getenv("DISPLAY", ":98"), **{advance_env: "1"})
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.{source}_recon --job {job} --keep 8"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return ((e.stdout or "") + (e.stderr or "")) if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, submitted_unconfirmed, needs_account, error, pending}."""
    if "application CONFIRMED" in log:
        return "confirmed", "real ack in the persona Maildir"
    if "SUBMIT clicked" in log:
        return "submitted_unconfirmed", "submit clicked, no ack within --keep (may lag)"
    if "CEILING: stopped at the" in log or "needs_account=True" in log:
        return "needs_account", "reached the account wall; account-create not completed"
    if "run error" in log or log.startswith("DRIVE-ERROR") or log == "TIMEOUT":
        return "error", "drive errored/timed out"
    return "pending", "drive did not reach a decisive step"


def run_once(source: str, advance_env: str, force_job: int | None = None,
             force: bool = False, dry: bool = False) -> dict:
    if already_verified(source):
        LOG(f"{source}_probe_promote: already verified — nothing to do")
        return {"done": True}
    quiet, why = box_is_quiet()
    ids = force_job and [force_job] or rr.job_ids(source)
    if not ids:
        LOG(f"{source}_probe_promote: no active {source} jobs to probe")
        return {"none": True}
    job = ids[0]
    if not (quiet or force):
        LOG(f"{source}_probe_promote: box busy ({why}) — skip, retry next tick. next job={job}")
        return {"skipped": True, "why": why, "next": job}
    if dry:
        LOG(f"{source}_probe_promote DRY: would probe job {job}; {why}")
        return {"dry": True, "job": job}
    LOG(f"{source}_probe_promote: probing job={job} ({why})")
    log = _drive(source, advance_env, job)
    verdict, detail = classify(log)
    LOG(f"{source}_probe_promote: job {job} -> {verdict} ({detail})")
    if verdict == "confirmed":
        _mark(_verified_path(source), source, detail)
        LOG(f"{source}_probe_promote: PROMOTED {source} -> data/{source}_verified.json")
    elif verdict == "error":
        _mark(_blocked_path(source), source, detail)
    # needs_account / submitted_unconfirmed / pending → leave pending, retry a future quiet tick
    return {"source": source, "job": job, "verdict": verdict, "detail": detail}


def _cli(source: str, advance_env: str) -> None:
    ap = argparse.ArgumentParser(description=f"self-verifying probe for the {source} lane")
    ap.add_argument("--job", type=int, default=0, help="probe a specific mass_hiring_jobs id")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true", help="report the quiet gate + job, drive nothing")
    args = ap.parse_args()
    lock_path = os.path.join(REPO, "logs", f"{source}_probe_promote.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lf = open(lock_path, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG(f"{source}_probe_promote: another run holds the lock — exiting")
        return
    try:
        run_once(source, advance_env, force_job=args.job or None, force=args.force,
                 dry=args.dry_run)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass


def main() -> None:
    _cli("roberthalf", "ROBERTHALF_ADVANCE")


if __name__ == "__main__":
    main()
