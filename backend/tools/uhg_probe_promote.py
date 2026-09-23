"""Self-verifying WALL-WATCH probe for the UnitedHealth Group / Optum apply flow.

UHG is currently BLOCKED: the Taleo apply flow's account step is a workforce Azure AD SSO with NO
candidate self-registration (see uhg_recon for the full traced flow + fresh 2026-09-23 evidence). So
unlike the other probe_promote lanes there is NO application to submit and NO Maildir ack to wait on —
the wall sits BEFORE any form. This probe instead WATCHES for the wall to ever drop:

  * It runs ONLY when the box is genuinely quiet (load1 < QUIET_LOAD AND no jobfinder headful drive),
    else exits 0 (the next tick retries) — it never adds to a load spike. It's read-only regardless.
  * When quiet it does a LIGHT headless walk of ONE live UHG job (Radancy page -> external Taleo apply
    URL -> Privacy "I Accept" -> read the landing) via `uhg_recon.probe_apply_flow`. It NEVER registers
    and NEVER submits (there is nothing to submit — the account step is the blocker).
  * Verdict (pure, `uhg_recon.probe_verdict`):
      - blocked_sso        -> the expected reality: recorded to data/uhg_probe_blocked.json, NOT promoted.
      - self_register_open -> a native Taleo self-register form appeared (Azure SSO removed!): PROMOTES
        'uhg' into data/uhg_verified.json — the signal for a human to build the lane (compose
        TaleoStrategy, proven on TTEC, + verify_code.read_code for the emailed OTP). NO code edit here.
      - unknown            -> left pending, retried a future quiet tick.

`UHG_ADVANCE` is honored as a hard safety switch even though the probe is read-only: without it, the
probe will refuse to run any live browser walk at all (report-only from the pure state). Idempotent +
fcntl-locked. `--dry-run` reports the quiet gate + verified/blocked state, driving nothing.
Manual: `python -m backend.tools.uhg_probe_promote [--job-page <url>] [--force]`.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import uhg_recon  # noqa: E402

QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))
LOCK_PATH = os.path.join(REPO, "logs", "uhg_probe_promote.lock")
VERIFIED_PATH = os.path.join(REPO, "data", "uhg_verified.json")       # gitignored marker
BLOCKED_PATH = os.path.join(REPO, "data", "uhg_probe_blocked.json")   # gitignored
LANE = "uhg"
LOG = print


def _load1() -> float:
    try:
        return os.getloadavg()[0]
    except Exception:
        return 999.0


def _jobfinder_headful_drives() -> int:
    try:
        out = subprocess.run(["ps", "-eo", "cmd"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 99
    pat = re.compile(r"python3 -m backend\.tools\.(workday_recon|foundever_recon|"
                     r"smartrecruiters_recon|gainwell_recon|icims_recon|amazon_recon|afni_recon|"
                     r"taleo_recon|shl_assess_runner) .*--(job|drain)\b")
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
    cur = _read_json_set(VERIFIED_PATH)
    cur.add(lane)
    os.makedirs(os.path.dirname(VERIFIED_PATH), exist_ok=True)
    tmp = VERIFIED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(cur), f)
    os.replace(tmp, VERIFIED_PATH)


def mark_blocked(reason: str, lane: str = LANE) -> None:
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


def next_job_page() -> str | None:
    """A live UHG Radancy job-page URL to probe (any active row — the account wall is job-uniform)."""
    try:
        from backend.tools import mass_hiring as mh
        with mh.conn() as c, c.cursor() as cur:
            cur.execute("SELECT apply_url FROM mass_hiring_jobs "
                        "WHERE source='unitedhealth' AND apply_url ILIKE '%careers.unitedhealthgroup.com%' "
                        "ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def decide(verdict: str, detail: str) -> dict:
    """Apply the promote/block/pending decision for a probe verdict. PURE (no I/O side effects here;
    the caller performs the marker writes). Returns {action, verdict, detail}."""
    if verdict == "self_register_open":
        return {"action": "promote", "verdict": verdict, "detail": detail}
    if verdict == "blocked_sso":
        return {"action": "block", "verdict": verdict, "detail": detail}
    return {"action": "pending", "verdict": verdict, "detail": detail}


def run_once(force_job_page: str | None = None, force: bool = False, dry: bool = False) -> dict:
    if is_verified():
        LOG("uhg_probe_promote: lane already verified (data/uhg_verified.json) — nothing to do")
        return {"done": True, "verified": True}
    quiet, why = box_is_quiet()
    job_page = force_job_page or next_job_page()
    if job_page is None:
        LOG("uhg_probe_promote: no active UHG job to probe")
        return {"done": True, "no_job": True}
    advance = os.getenv("UHG_ADVANCE") in ("1", "true", "yes", "on")
    if dry:
        LOG(f"uhg_probe_promote DRY: would probe {job_page[:70]}; {why}; "
            f"UHG_ADVANCE={advance}; verified={is_verified()}")
        return {"dry": True, "job_page": job_page, "why": why, "advance": advance}
    if not advance:
        LOG("uhg_probe_promote: UHG_ADVANCE unset — report-only, no live browser walk "
            "(current state: BLOCKED — workforce Azure AD SSO, no self-registration)")
        return {"skipped": True, "why": "UHG_ADVANCE unset (report-only)"}
    if not (quiet or force):
        LOG(f"uhg_probe_promote: box busy ({why}) — skip, retry next tick")
        return {"skipped": True, "why": why}
    LOG(f"uhg_probe_promote: probing {job_page[:70]} ({why})")
    apply_url = uhg_recon._fetch_apply_url_for(job_page)
    if not apply_url:
        LOG("uhg_probe_promote: could not resolve the external Taleo apply URL — pending")
        return {"verdict": "unknown", "detail": "apply URL unresolved"}
    try:
        res = asyncio.run(uhg_recon.probe_apply_flow(apply_url))
    except Exception as e:  # noqa: BLE001
        LOG(f"uhg_probe_promote: probe crashed ({type(e).__name__}: {e}) — pending")
        return {"verdict": "unknown", "detail": "probe crashed"}
    dec = decide(res["verdict"], res["detail"])
    LOG(f"uhg_probe_promote: landing={res['landing_kind']} -> {dec['verdict']} ({dec['detail']})")
    if dec["action"] == "promote":
        add_verified()
        LOG("uhg_probe_promote: PROMOTED uhg -> verified (SSO wall dropped; build the lane)")
    elif dec["action"] == "block":
        mark_blocked(dec["detail"])
    return {"job_page": job_page, **dec, "landing_kind": res["landing_kind"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job-page", default=None, help="probe one specific UHG Radancy job-page URL")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the quiet gate + verified/blocked state, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("uhg_probe_promote: another run holds the lock — exiting")
        return
    try:
        run_once(force_job_page=args.job_page, force=args.force, dry=args.dry_run)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    main()
