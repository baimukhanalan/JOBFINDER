"""Self-scheduling Workday-tenant prober + auto-promoter (cron every ~30 min).

The register-captcha / full-submit verification of the collected-but-unpromoted Workday tenants
(sagility/highmark/cvshealth/humana) needs a QUIET :98 — a single headful create-account→wizard→
submit drive false-fails when the SHARED host is under an external-project CPU load spike (drives
hang mid-wizard). This cron solves that WITHOUT a human babysitting it:

  * It runs ONLY when the box is genuinely quiet (load1 < QUIET_LOAD AND no jobfinder headful drive),
    else it exits 0 immediately (the next cron tick retries) — so it never adds to a load spike.
  * When quiet it drives ONE still-unverified pending tenant to the on-page "Application Submitted"
    success page (workday_recon, WORKDAY_ADVANCE=1). On confirm it appends the tenant to the
    gitignored data/workday_verified_tenants.json, which `mass_hiring_apply_workday_cron.live_tenants()`
    unions into the live set — so the next apply-cron run drives it automatically. NO code edit.
  * A tenant that reaches the create-account with NO captcha but does NOT complete the submit is left
    UNVERIFIED (logged as needs-screener) — never promoted on partial evidence. A tenant that shows a
    real register captcha is recorded to data/workday_probe_blocked.json (needs a solver key + US IP).

Idempotent + fcntl-locked. `--dry-run` reports the quiet-gate decision + the next pending tenant
without driving. Manual: `python -m backend.tools.workday_probe_promote [--tenant sagility] [--force]`.
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

from backend.tools import mass_hiring_apply_workday_cron as wc  # noqa: E402

# tenant -> a live mass_hiring_jobs id to probe (the register step + wizard are tenant-uniform).
# Everise (BPO) + Devoted (MA payer) were collected onto the Workday CxS lane 2026-09-22 and routed
# via SUPPORTED_HOSTS + _MASSHIRING_HOST_RE + _TENANT, so the probe can drive their create-account
# step on a quiet :98 and AUTO-PROMOTE on a confirmed on-page submit (like Sagility) — no code edit.
# (GEICO is host-wired too but has 0 active rows right now, so it has no probe id yet; Oscar/Clover
# are Greenhouse boards, NOT Workday, so they are NOT probed here.)
# cigna (cigna.wd5) + elevance (elevancehealth.wd1) added 2026-09-22 — both are supported Workday CxS
# hosts routed to the mass-hiring create-account wizard, and were in workday_recon._BLOCKED with no
# live re-verify; the probe drives them on a quiet :98 and auto-promotes on a confirmed on-page submit.
PENDING: dict[str, int] = {"sagility": 15576, "highmark": 15561, "cvshealth": 1108, "humana": 16492,
                           "everise": 17085, "devoted": 17093, "cigna": 16481, "elevance": 15559}

QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))   # 1-min load must be below this
DRIVE_SECS = int(os.getenv("PROBE_DRIVE_SECS", "540"))    # hard cap per probe drive
LOCK_PATH = os.path.join(REPO, "logs", "workday_probe_promote.lock")
BLOCKED_PATH = os.path.join(REPO, "data", "workday_probe_blocked.json")
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
    # ACTIVE headful drives that own :98 (a --job/--drain run). NOT the persistent
    # `shl_assess_runner --watch` supervisor (idle poller — always present, would block forever).
    pat = re.compile(r"python3 -m backend\.tools\.(workday_recon|foundever_recon|"
                     r"smartrecruiters_recon|gainwell_recon|icims_recon|shl_assess_runner) "
                     r".*--(job|drain)\b")
    return sum(1 for ln in out.splitlines() if pat.search(ln))


def box_is_quiet() -> tuple[bool, str]:
    load = _load1()
    drives = _jobfinder_headful_drives()
    ok = load < QUIET_LOAD and drives <= 0
    return ok, f"load1={load:.1f}(<{QUIET_LOAD}) jobfinder_drives={drives}"


def next_pending() -> str | None:
    """First pending tenant not already verified/blocked."""
    verified = wc._read_verified()
    blocked = _read_json(BLOCKED_PATH)
    for t in PENDING:
        if t not in verified and t not in blocked:
            return t
    return None


def _read_json(path: str) -> set:
    try:
        with open(path) as f:
            v = json.load(f)
        return set(v) if isinstance(v, list) else set(v.keys()) if isinstance(v, dict) else set()
    except Exception:
        return set()


def _mark_blocked(tenant: str, reason: str) -> None:
    try:
        cur = {}
        if os.path.exists(BLOCKED_PATH):
            with open(BLOCKED_PATH) as f:
                cur = json.load(f) or {}
    except Exception:
        cur = {}
    cur[tenant] = reason
    os.makedirs(os.path.dirname(BLOCKED_PATH), exist_ok=True)
    tmp = BLOCKED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f)
    os.replace(tmp, BLOCKED_PATH)


def _drive(job: int) -> str:
    """Drive one create-account→submit; return the raw log text (never raises)."""
    env = dict(os.environ, DISPLAY=os.getenv("DISPLAY", ":98"),
               WORKDAY_ADVANCE="1", WORKDAY_DEBUG_SHOTS="1")
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.workday_recon --job {job} --keep 8"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, no_captcha_incomplete, captcha, no_create, error}."""
    has_captcha = bool(re.search(r"register captcha presence:.*(grecaptcha': True|enterprise': True|"
                                 r"'frames': [1-9]|sitekey': '[^n])", log))
    created = "create-account: created=True" in log
    confirmed = ("on-page success" in log or "confirmed=1" in log
                 or re.search(r"confirmed=True", log))
    if has_captcha:
        return "captcha", "register-step captcha present"
    if confirmed:
        return "confirmed", "on-page Application Submitted"
    if created:
        return "no_captcha_incomplete", "no captcha + created=True but submit not reached (screener/load)"
    return ("error", "drive did not reach create-account")


def run_once(force_tenant: str | None = None, force: bool = False, dry: bool = False) -> dict:
    quiet, why = box_is_quiet()
    tenant = force_tenant or next_pending()
    if tenant is None:
        LOG("workday_probe_promote: nothing pending (all verified/blocked)")
        return {"done": True}
    if not (quiet or force):
        LOG(f"workday_probe_promote: box busy ({why}) — skip, retry next tick. next={tenant}")
        return {"skipped": True, "why": why, "next": tenant}
    if dry:
        LOG(f"workday_probe_promote DRY: would probe {tenant} (job {PENDING.get(tenant)}); {why}")
        return {"dry": True, "tenant": tenant}
    job = PENDING[tenant]
    LOG(f"workday_probe_promote: probing {tenant} job={job} ({why})")
    log = _drive(job)
    verdict, detail = classify(log)
    LOG(f"workday_probe_promote: {tenant} -> {verdict} ({detail})")
    if verdict == "confirmed":
        wc.add_verified(tenant)
        LOG(f"workday_probe_promote: PROMOTED {tenant} -> live_tenants (data/workday_verified_tenants.json)")
    elif verdict == "captcha":
        _mark_blocked(tenant, detail)
    # no_captcha_incomplete / error → leave pending, retry a future quiet tick
    return {"tenant": tenant, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=None, help="probe one specific tenant (sagility/highmark/cvshealth/humana)")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true", help="report the quiet gate + next tenant, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("workday_probe_promote: another run holds the lock — exiting")
        return
    try:
        run_once(force_tenant=args.tenant, force=args.force, dry=args.dry_run)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    main()
