"""Self-verifying iCIMS-tenant prober + auto-promoter (cron every ~30 min).

The exact analogue of tools/workday_probe_promote.py, for the iCIMS lane. The base TP iCIMS lane is
proven; a NEW iCIMS tenant collected COLLECT-ONLY (Cotiviti, careers-cotiviti.icims.com — a DIFFERENT
tenant than Teleperformance, so its screener set may differ) must be LIVE-VERIFIED to land a real
"Thank You for Applying" ack before it is auto-applied. This cron does that WITHOUT a human babysitting:

  * It runs ONLY when the box is genuinely quiet (reuses the Workday probe's quiet gate: load1 <
    QUIET_LOAD AND no jobfinder headful drive), else exits 0 immediately (the next tick retries) — so
    it never adds to a load spike (the shared headful :98 the drives fight over).
  * When quiet it drives ONE still-unverified pending tenant via `icims_recon` (the SAME headful +
    NopeCHA path the live TP cron uses) with a FRESH synthetic persona to the real iCIMS
    "Thank You for Applying" email in the persona Maildir. On confirm it appends the tenant SOURCE to
    the gitignored data/icims_verified_sources.json, which `mass_hiring_apply_tp_cron.live_sources()`
    unions into the live set — the next catch-all apply-cron run then drives it. NO code edit.
  * A drive that fills but does NOT land the ack is left PENDING (retried a future quiet tick) — never
    promoted on partial evidence. Cotiviti is additive: TP (teleperformance) is never affected.

Idempotent + fcntl-locked. `--dry-run` reports the quiet gate + the next pending tenant + the job it
would drive, without driving. Manual: `python -m backend.tools.icims_probe_promote [--source cotiviti]
[--force]`.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db  # noqa: E402
from backend.tools import mass_hiring_apply_tp_cron as tp  # noqa: E402
from backend.tools import workday_probe_promote as wpp  # noqa: E402 — reuse the proven quiet gate

# iCIMS tenant SOURCES that are collected but not yet live-verified for auto-apply. Each is probed in
# order; the job to drive is picked at RUNTIME (the newest active row for that source), so a stale
# hardcoded job id can never send the probe at an expired posting.
PENDING_SOURCES: tuple[str, ...] = ("cotiviti", "selectquote")

DRIVE_MINUTES = int(os.getenv("ICIMS_PROBE_KEEP", "13"))     # --keep window for the icims_recon drive
DRIVE_SECS = DRIVE_MINUTES * 60 + 150                        # hard subprocess cap (recon + ack poll)
LOCK_PATH = os.path.join(REPO, "logs", "icims_probe_promote.lock")
LOG = print


def newest_active_job(source: str) -> int | None:
    """The newest active mass_hiring_jobs id for this iCIMS tenant source (highest id = newest), or
    None when the tenant has 0 active rows (⇒ the probe is a no-op, like the Workday probe)."""
    try:
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT id FROM mass_hiring_jobs WHERE source=%s AND active "
                "AND apply_url ILIKE %s ORDER BY id DESC LIMIT 1",
                (source, "%icims%"))
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def next_pending() -> str | None:
    """First pending tenant source not already verified (promoted)."""
    verified = tp._read_verified_sources()
    for s in PENDING_SOURCES:
        if s not in verified:
            return s
    return None


def _drive(job: int) -> str:
    """Drive ONE fresh iCIMS application via icims_recon (headful + NopeCHA, DIRECT), then poll the
    persona Maildir for the real "Thank You for Applying" ack and append a PROBE-CONFIRMED marker to
    the log so classify() sees the ground truth. Returns the raw drive log (never raises)."""
    env = dict(os.environ)
    env.update({
        "DISPLAY": os.getenv("DISPLAY", ":98"),
        "ICIMS_NOPECHA": "1",
        "ICIMS_PROXY": "",                 # DIRECT — NopeCHA solves the captcha regardless of IP
        "NOPECHA_KEY": tp._nopecha_key(),
    })
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.icims_recon "
           f"--job {job} --fresh --keep {DRIVE_MINUTES}"]
    started = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        log = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        log = ((e.stdout or "") + (e.stderr or "")) if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"
    # Ground-truth the ack the SAME way the live TP cron does: parse the persona address out of the
    # recon stdout, then walk that Maildir for the iCIMS "Thank You for Applying" confirmation (it can
    # land a minute or two after the submit click, i.e. after the recon subprocess exited).
    persona = tp._persona_email_from_output(log)
    if persona:
        local = persona.split("@", 1)[0]
        for _ in range(6):
            if tp._mailbox_has_confirmation(local, started):
                log += "\nPROBE-CONFIRMED: iCIMS Thank-You-for-Applying ack in persona Maildir\n"
                break
            time.sleep(10)
    return log


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, no_form, pending}.

    confirmed = a REAL iCIMS ack (icims_recon's own '[application CONFIRMED submitted]' marker OR our
    post-drive Maildir PROBE-CONFIRMED). no_form = the posting was expired / bounced to job-search
    (retry a future collect). pending = filled but no ack yet (captcha stall / later step) — retried."""
    if re.search(r"application CONFIRMED submitted|PROBE-CONFIRMED", log, re.I):
        return "confirmed", "iCIMS Thank-You-for-Applying ack"
    if re.search(r"posting expired|no application form|job-search page|no register form", log, re.I):
        return "no_form", "posting expired / no apply form (retry a future collect)"
    return "pending", "filled but no ack (captcha stall / incomplete / later step) — retry a quiet tick"


def run_once(force_source: str | None = None, force: bool = False, dry: bool = False) -> dict:
    quiet, why = wpp.box_is_quiet()
    source = force_source or next_pending()
    if source is None:
        LOG("icims_probe_promote: nothing pending (all verified)")
        return {"done": True}
    job = newest_active_job(source)
    if job is None:
        LOG(f"icims_probe_promote: {source} has 0 active rows — no-op")
        return {"noop": True, "source": source}
    if not (quiet or force):
        LOG(f"icims_probe_promote: box busy ({why}) — skip, retry next tick. next={source} job={job}")
        return {"skipped": True, "why": why, "next": source}
    if dry:
        LOG(f"icims_probe_promote DRY: would probe {source} (job {job}); {why}")
        return {"dry": True, "source": source, "job": job}
    LOG(f"icims_probe_promote: probing {source} job={job} ({why})")
    log = _drive(job)
    verdict, detail = classify(log)
    LOG(f"icims_probe_promote: {source} -> {verdict} ({detail})")
    if verdict == "confirmed":
        tp.add_verified_source(source)
        LOG(f"icims_probe_promote: PROMOTED {source} -> live_sources (data/icims_verified_sources.json)")
    # no_form / pending → leave pending, retry a future quiet tick
    return {"source": source, "job": job, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="probe one specific iCIMS tenant source (e.g. cotiviti)")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the quiet gate + next tenant + the job it would drive, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("icims_probe_promote: another run holds the lock — exiting")
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
