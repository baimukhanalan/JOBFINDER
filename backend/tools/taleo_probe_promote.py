"""Self-verifying Taleo-source prober + auto-promoter (cron every ~30 min).

The Taleo analogue of `tools/workday_probe_promote.py`. It wires the COLLECT-FIRST Taleo-family
tenants — **kaiser** (kp.taleo.net) and **percepta** (percepta.taleo.net) — into the auto-apply
catch-all lane the same SAFE way the Workday tenants auto-promote: NOTHING goes live without a real
application receipt.

  * It runs ONLY when the box is genuinely quiet (load1 < QUIET_LOAD AND no Taleo drive already
    running), else it exits 0 immediately (the next cron tick retries) — so it never piles onto a
    load spike or double-drives.
  * When quiet it drives ONE application for the next un-promoted source through `taleo_recon`
    (TALEO_HEADLESS=1 TALEO_ADVANCE=1) — a fresh synthetic persona placed in the job's state, walked
    the whole JSF wizard to Submit, then a `--keep`-minute Maildir poll for the real receipt.
  * ONLY on a confirmed Maildir receipt (taleo_recon reports "application CONFIRMED submitted") does
    it append the source to the gitignored data/taleo_verified_sources.json, which
    `mass_hiring_apply_taleo_cron.live_sources()` unions into the live set — so the next catch-all
    cron run (`--exclude ttec`) drives it automatically. NO code edit.
  * A drive that reaches an on-page Submit but no Maildir receipt within --keep, or stalls on a
    tenant-specific Basics field, is left PENDING (logged) — never promoted on partial evidence. A
    mis-mapped Kaiser/Percepta Basics select just leaves a required field unset → the wizard bounces
    and no receipt lands → the source stays pending (fails safe, never mis-submits).

Taleo has NO captcha/WAF, so a drive is HEADLESS (no :98) — but it still uses CPU + the persona
mailbox, so the quiet-gate keeps it from fighting the live apply lanes. Idempotent + fcntl-locked.
`--dry-run` reports the quiet gate + the next pending source + the live job it would drive, without
driving. Manual: `python -m backend.tools.taleo_probe_promote [--source kaiser] [--force]`.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db  # noqa: E402
from backend.tools import mass_hiring_apply_taleo_cron as tc  # noqa: E402

# Collected COLLECT-FIRST onto the Taleo lane (reuse strategies/taleo.py). Probed newest-first — the
# live job id is picked at RUNTIME (no hardcoded id), so a drained/closed board just no-ops.
PENDING: tuple[str, ...] = ("kaiser", "percepta")

QUIET_LOAD = float(os.getenv("PROBE_QUIET_LOAD", "9"))       # 1-min load must be below this
KEEP_MIN = int(os.getenv("PROBE_KEEP_MIN", "10"))            # Maildir-receipt poll window per drive
DRIVE_SECS = int(os.getenv("PROBE_DRIVE_SECS", str(KEEP_MIN * 60 + 180)))  # hard cap per probe drive
LOCK_PATH = os.path.join(REPO, "logs", "taleo_probe_promote.lock")
LOG = print


def _load1() -> float:
    try:
        return os.getloadavg()[0]
    except Exception:
        return 999.0


def _taleo_drives() -> int:
    """Count LIVE Taleo apply drives (a `taleo_recon --job` subprocess) — a probe must not double-
    drive or stack on the apply cron."""
    try:
        out = subprocess.run(["ps", "-eo", "cmd"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 99
    pat = re.compile(r"python3 -m backend\.tools\.taleo_recon .*--job\b")
    return sum(1 for ln in out.splitlines() if pat.search(ln))


def box_is_quiet() -> tuple[bool, str]:
    load = _load1()
    drives = _taleo_drives()
    ok = load < QUIET_LOAD and drives <= int(os.getenv("PROBE_MAX_DRIVES", "0"))
    return ok, f"load1={load:.1f}(<{QUIET_LOAD}) taleo_drives={drives}"


def next_pending() -> str | None:
    """First PENDING source not already promoted (in live_sources())."""
    live = tc.live_sources()
    for s in PENDING:
        if s not in live:
            return s
    return None


def live_probe_job(source: str) -> int | None:
    """The NEWEST active, honestly-staffable mass_hiring_jobs id for `source` (no hardcoded id).
    None when the source has 0 active applyable rows (the probe then no-ops for it)."""
    from backend.tools.synth_persona import job_is_staffable
    from backend.tools.taleo_recon import _TTEC_LICENSED_IDS, is_licensed
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs WHERE source=%s AND active "
                    "ORDER BY id DESC", (source,))
        rows = cur.fetchall()
    for jid, title in rows:
        if jid in _TTEC_LICENSED_IDS or is_licensed(title):
            continue
        if not job_is_staffable({"title": title}):
            continue
        return jid
    return None


def _drive(job: int) -> str:
    """Drive ONE Taleo application headless (TALEO_ADVANCE=1); return the raw log (never raises)."""
    env = dict(os.environ, TALEO_HEADLESS="1", TALEO_ADVANCE="1")
    cmd = ["sg", "mail", "-c",
           f"cd {REPO} && PYTHONPATH=. python3 -m backend.tools.taleo_recon "
           f"--job {job} --fresh --keep {KEEP_MIN}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=DRIVE_SECS, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return f"DRIVE-ERROR {type(e).__name__}: {e}"


def classify(log: str) -> tuple[str, str]:
    """(verdict, detail). verdict ∈ {confirmed, submitted_no_ack, no_form, skipped, incomplete}.

    ONLY `confirmed` promotes — that is taleo_recon reporting a REAL Taleo application receipt in the
    persona Maildir (the TTEC-style "Thank you for applying" / "received your application" ack that
    `taleo_recon._app_confirmed` matches). An on-page Submit with no receipt, or a stall, stays
    pending (fails safe)."""
    if "application CONFIRMED submitted" in log:
        return "confirmed", "Taleo application receipt in the persona Maildir"
    if re.search(r"\bsubmitted=True\b", log):
        return "submitted_no_ack", "on-page Submit reached but no Maildir receipt within --keep"
    if "could not resolve a taleo.net apply URL" in log:
        return "no_form", "could not resolve the taleo.net apply URL"
    if re.search(r"\[skip:", log):
        return "skipped", "row skipped (licensed / unstaffable)"
    return "incomplete", "drive did not confirm (fill/selector stall — likely a tenant Basics gap)"


def run_once(force_source: str | None = None, force: bool = False, dry: bool = False) -> dict:
    quiet, why = box_is_quiet()
    source = force_source or next_pending()
    if source is None:
        LOG("taleo_probe_promote: nothing pending (all sources promoted)")
        return {"done": True}
    if not (quiet or force):
        LOG(f"taleo_probe_promote: box busy ({why}) — skip, retry next tick. next={source}")
        return {"skipped": True, "why": why, "next": source}
    job = live_probe_job(source)
    if job is None:
        LOG(f"taleo_probe_promote: {source} has 0 active applyable rows — no-op this tick")
        return {"source": source, "verdict": "no_rows"}
    if dry:
        LOG(f"taleo_probe_promote DRY: would probe {source} job={job}; {why}")
        return {"dry": True, "source": source, "job": job}
    LOG(f"taleo_probe_promote: probing {source} job={job} ({why})")
    log = _drive(job)
    verdict, detail = classify(log)
    LOG(f"taleo_probe_promote: {source} -> {verdict} ({detail})")
    if verdict == "confirmed":
        tc.add_verified_source(source)
        LOG(f"taleo_probe_promote: PROMOTED {source} -> live_sources (data/taleo_verified_sources.json)")
    # submitted_no_ack / no_form / skipped / incomplete → leave pending, retry a future quiet tick
    return {"source": source, "job": job, "verdict": verdict, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="probe one specific source (kaiser/percepta)")
    ap.add_argument("--force", action="store_true", help="probe even if the box isn't quiet")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the quiet gate + next source + the live job, drive nothing")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG("taleo_probe_promote: another run holds the lock — exiting")
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
