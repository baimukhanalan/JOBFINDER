"""Self-verifying Avature-tenant prober + auto-promoter (cron every ~30 min).

The exact analogue of tools/workday_probe_promote.py, for the Avature lane. The base Maximus lane is
proven; a NEW Avature tenant collected COLLECT-ONLY (Transcom, apply.careers.transcom.com — a DIFFERENT
tenant than Maximus, so its `_SCREENERS` set may differ) must be LIVE-VERIFIED to land a real
account-created/submit ack before it is auto-applied. This cron does that WITHOUT a human babysitting:

  * It runs ONLY when the box is genuinely quiet (reuses the Workday probe's quiet gate: load1 <
    QUIET_LOAD AND no jobfinder headful drive), else exits 0 immediately (the next tick retries).
  * When quiet it drives ONE still-unverified pending tenant through the SAME Avature apply path the
    live Maximus cron uses — `mass_hiring_apply.run_batch_parallel([job], dry_run=False)` with
    AVATURE_ADVANCE=1 — a fresh synthetic persona that walks the Register wizard to the final Submit.
    The co-pilot reports `confirmed` on the on-page account-created/submit success. On confirm it
    appends the tenant SOURCE to the gitignored data/avature_verified_sources.json, which
    `mass_hiring_apply_cron.live_sources()` unions in — the next catch-all apply-cron run drives it.
    NO code edit. Transcom is additive: Maximus is never affected.
  * A drive that fills/clicks but does NOT confirm is left PENDING (retried) — never promoted on
    partial evidence; the documented Avature dead-lane symptom (a newly-required step-1 screener leaves
    `unfilled` non-empty → the submit gate refuses) surfaces as `clicked=None`, i.e. still pending.

Idempotent + fcntl-locked. `--dry-run` reports the quiet gate + next tenant + the job it would drive.
Manual: `python -m backend.tools.avature_probe_promote [--source transcom] [--force]`.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db  # noqa: E402
from backend.tools import mass_hiring_apply_cron as mc  # noqa: E402
from backend.tools import workday_probe_promote as wpp  # noqa: E402 — reuse the proven quiet gate

# Avature tenant SOURCES that are collected but not yet live-verified for auto-apply. Each is probed in
# order; the job to drive is picked at RUNTIME (the newest active row for that source).
PENDING_SOURCES: tuple[str, ...] = ("transcom",)

DRIVE_SECS = int(os.getenv("AVATURE_PROBE_DRIVE_SECS", "420"))   # per-job timeout for the headful drive
LOCK_PATH = os.path.join(REPO, "logs", "avature_probe_promote.lock")
LOG = print


def newest_active_job(source: str) -> int | None:
    """The newest active mass_hiring_jobs id for this Avature tenant source (highest id = newest), or
    None when the tenant has 0 active rows (⇒ the probe is a no-op)."""
    try:
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT id FROM mass_hiring_jobs WHERE source=%s AND active ORDER BY id DESC LIMIT 1",
                (source,))
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def next_pending() -> str | None:
    """First pending tenant source not already verified (promoted)."""
    verified = mc._read_verified_sources()
    for s in PENDING_SOURCES:
        if s not in verified:
            return s
    return None


def _drive(job: int) -> dict:
    """Drive ONE fresh Avature application via the proven apply path (headful bulk_pool worker,
    AVATURE_ADVANCE=1, dry_run=False so it walks the wizard to Submit). Returns the per-job result
    dict (with `confirmed`/`clicked`/`error`); never raises."""
    os.environ["AVATURE_ADVANCE"] = "1"   # read by the strategy at import in the headless worker
    try:
        from backend.tools import mass_hiring_apply as mha
        res = mha.run_batch_parallel([job], workers=1, gender=None,
                                     dry_run=False, per_job_timeout=DRIVE_SECS)
        return res[0] if res else {"error": "no result"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def classify(res: dict) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, error, clicked_no_ack, pending}.

    confirmed = the co-pilot recorded a real on-page account-created/submit ack (`confirmed` truthy) →
    promote. Everything else stays PENDING (not promoted): a transport error, a filled-and-clicked
    submit with no confirmation yet, or an incomplete fill (the dead-lane screener symptom)."""
    res = res or {}
    if res.get("confirmed"):
        return "confirmed", "Avature account-created / submit ack"
    if res.get("error"):
        return "error", str(res.get("error"))[:120]
    if res.get("clicked"):
        return "clicked_no_ack", "submit clicked but no ack yet — retry a quiet tick"
    return "pending", "filled but not submitted (incomplete screener / gate) — retry a quiet tick"


def run_once(force_source: str | None = None, force: bool = False, dry: bool = False) -> dict:
    quiet, why = wpp.box_is_quiet()
    source = force_source or next_pending()
    if source is None:
        LOG("avature_probe_promote: nothing pending (all verified)")
        return {"done": True}
    job = newest_active_job(source)
    if job is None:
        LOG(f"avature_probe_promote: {source} has 0 active rows — no-op")
        return {"noop": True, "source": source}
    if not (quiet or force):
        LOG(f"avature_probe_promote: box busy ({why}) — skip, retry next tick. next={source} job={job}")
        return {"skipped": True, "why": why, "next": source}
    if dry:
        LOG(f"avature_probe_promote DRY: would probe {source} (job {job}); {why}")
        return {"dry": True, "source": source, "job": job}
    LOG(f"avature_probe_promote: probing {source} job={job} ({why})")
    res = _drive(job)
    verdict, detail = classify(res)
    LOG(f"avature_probe_promote: {source} -> {verdict} ({detail})")
    if verdict == "confirmed":
        mc.add_verified_source(source)
        LOG(f"avature_probe_promote: PROMOTED {source} -> live_sources (data/avature_verified_sources.json)")
    # error / clicked_no_ack / pending → leave pending, retry a future quiet tick
    return {"source": source, "job": job, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="probe one specific Avature tenant source (e.g. transcom)")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the quiet gate + next tenant + the job it would drive, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("avature_probe_promote: another run holds the lock — exiting")
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
