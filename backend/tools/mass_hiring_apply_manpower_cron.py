"""Cron: real-submit to every auto-applyable ManpowerGroup (Manpower + Experis) job once per run.

The Manpower analogue of the Foundever/TTEC lane, but SERVER-SIDE (no browser): the guest apply is a
plain multipart POST to `JobApplyWithEmail` (NO auth/CSRF/B2C/captcha/résumé — see
`strategies/manpower.py`), driven by `backend.tools.manpower_recon` with `MANPOWER_ADVANCE=1`. Each job
gets a fresh synthetic persona whose @takhet.com mailbox catches any recruiter reply; the API
`status==1000` (+entityID) is the "application received" ground truth (like Foundever's on-page ack).

    python3 backend/tools/mass_hiring_apply_manpower_cron.py                 # 1 application per job
    MANPOWER_ADVANCE=1 python3 backend/tools/mass_hiring_apply_manpower_cron.py --limit 4
    python3 backend/tools/mass_hiring_apply_manpower_cron.py --source experis --only 15214

Run under `sg mail` (mailbox provisioning + the Maildir read need the mail group). No DISPLAY/:98 —
this lane never opens a browser, so it doesn't contend with the headful :98 lanes."""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("manpower_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

LOCK_PATH = os.path.join(REPO, "logs", "manpower_apply.lock")


def _do_one(jobid: int, keep: int) -> dict:
    from backend.tools import manpower_recon
    try:
        rep = manpower_recon.run(jobid, keep_minutes=keep)
    except Exception as e:  # noqa: BLE001
        logger.info("applied job %s -> ERROR %s: %s", jobid, type(e).__name__, e)
        return {"jobid": jobid, "error": str(e)}
    logger.info("applied job %s -> advanced=%s success=%s entity_id=%s note=%s",
                jobid, rep.get("advanced"), rep.get("success"), rep.get("entity_id"), rep.get("note"))
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N jobs")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--source", default="", help="restrict to 'manpower' or 'experis'")
    ap.add_argument("--keep", type=int, default=6, help="minutes cap per application (Maildir wait)")
    ap.add_argument("--rounds", type=int, default=1, help="applications per job this run")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous Manpower apply run is still going — exiting")
        return

    from backend.tools import manpower_recon
    if args.only:
        ids = [args.only]
    else:
        ids = manpower_recon.manpower_job_ids(args.source or None)
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no auto-applyable Manpower/Experis jobs on the board")
        return

    # High-pay-first + STOP-ON-RESPONSE + `rounds` personas/run (guarded; falls back to ids*rounds).
    from backend.tools import offer_priority
    batch = offer_priority.plan_mh_batch(ids, rounds=max(1, args.rounds))
    if not batch:
        logger.info("every Manpower/Experis job already reached interview/offer — nothing to apply")
        return
    logger.info("applying to %d Manpower/Experis jobs x %d round(s) = %d applications (pay-ordered)",
                len(ids), args.rounds, len(batch))
    results = [_do_one(j, args.keep) for j in batch]
    submitted = sum(1 for r in results if r.get("success"))
    logger.info("manpower apply run done: %d applications, succeeded=%d", len(batch), submitted)


if __name__ == "__main__":
    main()
