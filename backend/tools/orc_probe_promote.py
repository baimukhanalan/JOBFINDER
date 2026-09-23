"""Self-scheduling Oracle-ORC source prober + auto-promoter (cron every ~30 min).

The Oracle Recruiting Cloud (ORC) analogue of tools/workday_probe_promote.py. The collect-only
ORC tenants molina + hilton share Alorica's `OracleORCStrategy` + login-less guest apply, but their
per-tenant screener battery MAY differ, so they are NOT driven live until a REAL Oracle application
ack proves the fill works end-to-end. This cron does that verification WITHOUT a human babysitting it:

  * It runs ONLY when the box is genuinely quiet (load1 < QUIET_LOAD AND no jobfinder headful drive),
    else it exits 0 immediately (the next tick retries) — the ORC fill is headful on the shared :98,
    so it must never add to a load spike.
  * When quiet it drives ONE still-unverified pending source's NEWEST active applyable job to a real
    Maildir ack (orc_recon, ORC_ADVANCE=1). On a confirmed Oracle receipt it appends the source to
    the gitignored data/orc_verified_sources.json, which `mass_hiring_apply_orc_cron.live_sources()`
    unions into the driven set — so the catch-all apply cron picks it up automatically. NO code edit.
  * A source with 0 active applyable rows (hilton today) is a NO-OP (like geico in the Workday
    probe). A drive that reaches the form but does NOT confirm is left PENDING (retry a future quiet
    tick — never promoted on partial evidence). A drive that hits a login/captcha wall is recorded
    to data/orc_probe_blocked.json.

Idempotent + fcntl-locked. `--dry-run` reports the quiet gate + next source without driving.
Manual: `python -m backend.tools.orc_probe_promote [--source molina] [--force]`.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mass_hiring_apply_orc_cron as oc  # noqa: E402

# The collect-only ORC tenants to verify. Unlike the Workday probe (which pins a job id per tenant),
# an ORC source's probe job is chosen at RUNTIME (the board churns) — the NEWEST active applyable row
# for the source. A source with no active row is a no-op (hilton has 0 rows right now).
PENDING: tuple[str, ...] = ("molina", "hilton")

QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))    # 1-min load must be below this
DRIVE_SECS = int(os.getenv("PROBE_DRIVE_SECS", "780"))    # hard cap per probe drive (ORC keep=10m)
KEEP_MIN = int(os.getenv("PROBE_KEEP_MIN", "10"))         # orc_recon --keep (Maildir ack poll)
LOCK_PATH = os.path.join(REPO, "logs", "orc_probe_promote.lock")
BLOCKED_PATH = os.path.join(REPO, "data", "orc_probe_blocked.json")
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
    pat = re.compile(r"python3 -m backend\.tools\.(orc_recon|workday_recon|foundever_recon|"
                     r"smartrecruiters_recon|gainwell_recon|icims_recon|shl_assess_runner) "
                     r".*--(job|drain)\b")
    return sum(1 for ln in out.splitlines() if pat.search(ln))


def box_is_quiet() -> tuple[bool, str]:
    load = _load1()
    drives = _jobfinder_headful_drives()
    ok = load < QUIET_LOAD and drives <= int(os.getenv("PROBE_MAX_DRIVES", "0"))
    return ok, f"load1={load:.1f}(<{QUIET_LOAD}) jobfinder_drives={drives}"


def _read_json(path: str) -> set:
    try:
        with open(path) as f:
            v = json.load(f)
        return set(v) if isinstance(v, list) else set(v.keys()) if isinstance(v, dict) else set()
    except Exception:
        return set()


def _probe_job_for(source: str) -> int | None:
    """The NEWEST active, staffable mass_hiring_jobs id for this ORC source (do NOT hardcode a job
    id — the board churns). None when the source has no active applyable row (a no-op, like geico
    in the Workday probe). Guarded — never raises into the tick."""
    try:
        from backend.tools import mail_db
        from backend.tools.synth_persona import job_is_staffable
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id, title FROM mass_hiring_jobs WHERE source=%s AND active "
                        "ORDER BY id DESC", (source,))
            for jid, title in cur.fetchall():
                if job_is_staffable({"title": title}):
                    return jid
    except Exception:
        return None
    return None


def next_pending() -> str | None:
    """First pending source not already verified/blocked that HAS a live job to probe."""
    verified = oc._read_verified()
    blocked = _read_json(BLOCKED_PATH)
    for s in PENDING:
        if s in verified or s in blocked:
            continue
        if _probe_job_for(s) is not None:
            return s
    return None


def _mark_blocked(source: str, reason: str) -> None:
    try:
        cur = {}
        if os.path.exists(BLOCKED_PATH):
            with open(BLOCKED_PATH) as f:
                cur = json.load(f) or {}
    except Exception:
        cur = {}
    cur[source] = reason
    os.makedirs(os.path.dirname(BLOCKED_PATH), exist_ok=True)
    tmp = BLOCKED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f)
    os.replace(tmp, BLOCKED_PATH)


def _drive(job: int) -> str:
    """Drive one ORC guest apply → Submit → Maildir-ack watch; return the raw log text (never
    raises). Headful on :98, ORC_ADVANCE=1. Wrapped in `sg mail` (mailbox provisioning + the
    emailed PIN + the Maildir ack read), mirroring the Workday probe's _drive."""
    env = dict(os.environ, DISPLAY=os.getenv("DISPLAY", ":98"), ORC_ADVANCE="1")
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.orc_recon "
           f"--job {job} --fresh --keep {KEEP_MIN}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, wall, incomplete, error}.

    The ORC ceiling is the on-page Redwood Submit + a real Oracle receipt in the persona's Maildir;
    orc_recon prints `[application CONFIRMED — Oracle receipt in the Maildir]` on that ground truth.
    Anything short of it is NOT promoted (honest by design)."""
    if re.search(r"application CONFIRMED|Oracle receipt in the Maildir", log):
        return "confirmed", "Oracle receipt in the Maildir"
    # Oracle CX guest apply is login-less with no interactive captcha, but be defensive: a page the
    # analyzer classifies terminal (login/captcha/expired) is a genuine wall, not a screener miss.
    if re.search(r"page_type=(?:login_required|captcha|expired)", log):
        return "wall", "apply page walled (login/captcha/expired)"
    # Reached + filled the form but no ack within --keep -> a screener/postal residual or slow load;
    # leave pending and retry a future quiet tick (never a wall).
    if re.search(r"\[filled:", log) or "orc apply done" in log:
        return "incomplete", "reached form, no ack (screener/postal residual or slow load)"
    return "error", "drive did not reach the form"


def run_once(force_source: str | None = None, force: bool = False, dry: bool = False) -> dict:
    quiet, why = box_is_quiet()
    source = force_source or next_pending()
    if source is None:
        LOG("orc_probe_promote: nothing pending (all verified/blocked or no active rows)")
        return {"done": True}
    job = _probe_job_for(source)
    if job is None:
        LOG(f"orc_probe_promote: {source} has no active applyable job — no-op, retry next tick")
        return {"noop": True, "source": source}
    if not (quiet or force):
        LOG(f"orc_probe_promote: box busy ({why}) — skip, retry next tick. next={source} job={job}")
        return {"skipped": True, "why": why, "next": source}
    if dry:
        LOG(f"orc_probe_promote DRY: would probe {source} (job {job}); {why}")
        return {"dry": True, "source": source, "job": job}
    LOG(f"orc_probe_promote: probing {source} job={job} ({why})")
    log = _drive(job)
    verdict, detail = classify(log)
    LOG(f"orc_probe_promote: {source} -> {verdict} ({detail})")
    if verdict == "confirmed":
        oc.add_verified(source)
        LOG(f"orc_probe_promote: PROMOTED {source} -> live_sources (data/orc_verified_sources.json)")
    elif verdict == "wall":
        _mark_blocked(source, detail)
    # incomplete / error -> leave pending, retry a future quiet tick
    return {"source": source, "job": job, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="probe one specific source (molina/hilton)")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the quiet gate + next source, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("orc_probe_promote: another run holds the lock — exiting")
        return
    try:
        run_once(force_source=args.source, force=args.force, dry=args.dry_run)
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    main()
