"""Cron: real-submit to every Maximus (Avature) job once per run.

Scheduled 5x/day -> 5 applications per job per day (each application is a fresh synthetic persona,
so it generates a fresh SHL assessment invite). Lock-guarded so overlapping runs never stack.

    python backend/tools/mass_hiring_apply_cron.py            # 1 application per Maximus job
    python backend/tools/mass_hiring_apply_cron.py --rounds 5 # 5 per job in one run
    python backend/tools/mass_hiring_apply_cron.py --workers 4

Headful workers (bulk_pool, ports 8110+) — run under `sg mail` (mailbox provisioning + emailed-code).
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("mh_apply_cron")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, mass_hiring_apply as mha  # noqa: E402

LOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "mh_apply_cron.lock")


def maximus_ids() -> list[int]:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id FROM mass_hiring_jobs WHERE apply_url ILIKE %s ORDER BY id", ("%avature%",))
        from backend.tools import mh_settings
        return mh_settings.drop_spanish([r[0] for r in cur.fetchall()])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=1, help="applications per Maximus job this run")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous apply run is still going — exiting")
        return

    ids = maximus_ids()
    if not ids:
        logger.info("no Maximus (avature) jobs on the board")
        return
    batch = ids * max(1, args.rounds)  # one application per id per round
    logger.info("applying to %d Maximus jobs x %d round(s) = %d applications", len(ids), args.rounds, len(batch))
    res = mha.run_batch_parallel(batch, workers=args.workers, gender=None,
                                 dry_run=False, per_job_timeout=420)
    conf = sum(1 for r in res if r.get("confirmed"))
    clicked = sum(1 for r in res if r.get("clicked"))
    logger.info("apply run done: %d jobs, clicked=%d, confirmed=%d", len(res), clicked, conf)


if __name__ == "__main__":
    main()
