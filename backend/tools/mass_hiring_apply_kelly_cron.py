"""Cron: real-submit to every Kelly (KellyConnect) job once per run.

The Kelly analogue of the Maximus (Avature) lane. Kelly job pages (www.mykelly.com/job/<id>-…)
embed a login-less WordPress Gravity Form backed by Bullhorn with NO submit captcha and NO account
wall — the ONLY gate is Akamai bot-management on the host, which 403s our datacenter IP. Verified
live 2026-09-02: the apply page returns 200 + the Gravity Form through the rotating BD proxy-pool
gateway (the SAME datacenter `alibaba_dc` egress that already clears Akamai for the Kelly job FEED —
a US RESIDENTIAL zone is NOT required, correcting the earlier strategies/kelly.py note). So each
application is driven through a fresh headless worker whose browser CONTEXT is routed through a pool
proxy — `mass_hiring_apply.run_batch_parallel` does this automatically for mykelly.com hosts (see
`_PROXY_APPLY_HOSTS`) and retries with a fresh egress if a bad IP still 403s.

The co-pilot picks `KellyStrategy` by URL, fills the whole Gravity Form (identity + résumé + the CSR
screeners the strategy answers deterministically) and — because the worker sets KELLY_ADVANCE=1 —
records + clicks the final Submit (self-gated: it never clicks if a required field is unfilled or a
synthetic answer needs review). A confirmed submit lands a Bullhorn/Kelly acknowledgement in the
persona's @takhet.com box.

    python backend/tools/mass_hiring_apply_kelly_cron.py             # 1 application per Kelly job
    python backend/tools/mass_hiring_apply_kelly_cron.py --only 800  # just this one
    python backend/tools/mass_hiring_apply_kelly_cron.py --workers 2 --rounds 2

Run under `sg mail` (mailbox provisioning + the Maildir confirmation read). NOTE the fill is heavier
than the other lanes — the 894 KB Gravity-Forms page loads through a proxy and the fill makes several
local-LLM calls — so `per_job_timeout` is 600 s (a slow/saturated LLM pushes a single fill past the
420 s the Maximus lane uses) and the worker default is low (2) so parallel Kelly fills don't starve
the one local LLM. Kelly's apply depends on a LIVE proxy pool; with an empty pool the load 403s and
the run no-ops (logged), same as the Kelly feed.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("kelly_apply_cron")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, mass_hiring_apply as mha  # noqa: E402

LOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "kelly_apply_cron.lock")


def kelly_ids() -> list[int]:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM mass_hiring_jobs WHERE active AND apply_url ILIKE %s ORDER BY id",
                    ("%mykelly%",))
        from backend.tools import mh_settings
        return mh_settings.drop_spanish([r[0] for r in cur.fetchall()])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel headless workers (low by default — Kelly fills share the one local LLM)")
    ap.add_argument("--rounds", type=int, default=1, help="applications per Kelly job this run")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--per-job-timeout", type=int, default=600,
                    help="seconds per application (Kelly's proxied Gravity-Forms fill is slow)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous Kelly apply run is still going — exiting")
        return

    ids = [args.only] if args.only else kelly_ids()
    if not ids:
        logger.info("no Kelly (mykelly) jobs on the board")
        return
    batch = ids * max(1, args.rounds)  # one application per id per round
    logger.info("applying to %d Kelly jobs x %d round(s) = %d applications", len(ids), args.rounds, len(batch))
    res = mha.run_batch_parallel(batch, workers=args.workers, gender=None,
                                 dry_run=False, per_job_timeout=args.per_job_timeout)
    conf = sum(1 for r in res if r.get("confirmed"))
    clicked = sum(1 for r in res if r.get("clicked"))
    errs = sum(1 for r in res if r.get("error"))
    logger.info("Kelly apply run done: %d jobs, clicked=%d, confirmed=%d, errors=%d",
                len(res), clicked, conf, errs)


if __name__ == "__main__":
    main()
