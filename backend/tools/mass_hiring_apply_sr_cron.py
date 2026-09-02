"""Cron: real-submit to every auto-applyable SmartRecruiters (guest one-click) job once per run.

The SmartRecruiters analogue of the TTEC / Maximus lanes. SmartRecruiters postings are fronted by
DataDome bot-management, so each application is driven HEADFUL under `DISPLAY=:98` by
`backend.tools.smartrecruiters_recon` (patchright stealth clears DataDome; the one-click form is a
shadow-DOM web-component app the strategy fills), which lands a real "application received" email in
the persona's @takhet.com box. Each job gets its OWN `SR_PROFILE_DIR`, so `--workers` runs can go in
parallel — several stealth browsers coexist on the shared `:98` display (they drive via CDP, not focus).

    python backend/tools/mass_hiring_apply_sr_cron.py                 # 1 application per doable job
    python backend/tools/mass_hiring_apply_sr_cron.py --only 536      # just one
    python backend/tools/mass_hiring_apply_sr_cron.py --workers 2

Run under `sg mail` + `DISPLAY=:98` (the recon provisions the persona mailbox + reads the Maildir
confirmation). The subprocess inherits the group/display — do NOT re-wrap it in another `sg mail`.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("sr_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db  # noqa: E402

LOCK_PATH = os.path.join(REPO, "logs", "sr_apply.lock")
_RESULT_RE = re.compile(r"^\[result\]\s*(\{.*\})\s*$", re.M)


def sr_job_ids() -> list[int]:
    """Active SmartRecruiters jobs we can honestly staff (English CSR — no license/exotic-language gate
    on this source today; `job_is_staffable` still vetoes anything requiring a language we can't fill).

    Selected by the apply_url HOST, not `source`: SmartRecruiters postings on this board carry
    source='sutherland' (the employer), not 'smartrecruiters' — the ATS is identified by the
    jobs.smartrecruiters.com apply_url (mirrors the TP lane keying on '%icims%')."""
    from backend.tools.synth_persona import job_is_staffable
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs "
                    "WHERE apply_url ILIKE '%smartrecruiters.com%' AND active ORDER BY id")
        for jid, title in cur.fetchall():
            if job_is_staffable({"title": title}):
                out.append(jid)
    return out


def apply_one(jobid: int, keep: int) -> dict:
    """Drive ONE SmartRecruiters application via smartrecruiters_recon (headful :98, fresh persona,
    isolated profile dir). Returns the recon's own {jobid, persona, ack, subject, ...}. Never raises."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":98")
    env.update({"SMARTRECRUITERS_ADVANCE": "1",
                "SR_PROFILE_DIR": os.path.join(REPO, "backend", "data", f"sr_stealth_profile_{os.getpid()}_{jobid}")})
    out = ""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backend.tools.smartrecruiters_recon", "--job", str(jobid), "--keep", str(keep)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=keep * 60 + 180)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        try:
            subprocess.run(["pkill", "-f", f"smartrecruiters_recon --job {jobid}"], timeout=20)
        except Exception:
            pass
        return {"jobid": jobid, "persona": None, "ack": False, "error": "timeout"}
    except Exception as e:
        return {"jobid": jobid, "persona": None, "ack": False, "error": f"{type(e).__name__}: {e}"}
    m = _RESULT_RE.search(out or "")
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {"jobid": jobid, "persona": None, "ack": False, "error": "no result line"}


def _do_one(jobid: int, keep: int) -> dict:
    res = apply_one(jobid, keep)
    logger.info("applied job %s persona=%s -> ack=%s%s",
                jobid, res.get("persona"), res.get("ack"),
                f" ERROR {res['error']}" if res.get("error") else "")
    return res


def _confirmed_jobids_in_log() -> set:
    ids = set()
    try:
        with open(os.path.join(REPO, "logs", "sr_apply.log")) as f:
            for line in f:
                m = re.search(r"applied job (\d+).*ack=True", line)
                if m:
                    ids.add(int(m.group(1)))
    except Exception:
        pass
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N jobs (0 = all)")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--keep", type=int, default=8, help="minutes cap per application")
    ap.add_argument("--workers", type=int, default=1, help="concurrent applications (per-job profile dir)")
    ap.add_argument("--skip-confirmed", action="store_true",
                    help="skip jobids already ack=True in sr_apply.log (resume a partial pass)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous SmartRecruiters apply run is still going — exiting")
        return

    if args.only:
        ids = [args.only]
    else:
        ids = sr_job_ids()
        if args.skip_confirmed:
            done = _confirmed_jobids_in_log()
            ids = [i for i in ids if i not in done]
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no auto-applyable SmartRecruiters jobs on the board")
        return

    logger.info("SmartRecruiters apply: %d jobs, %d workers", len(ids), max(1, args.workers))
    started = time.time()
    workers = max(1, args.workers)
    if workers == 1:
        results = [_do_one(j, args.keep) for j in ids]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda j: _do_one(j, args.keep), ids))
    acks = sum(1 for r in results if r.get("ack"))
    logger.info("FINISHED: %d/%d acked in %.0fs", acks, len(results), time.time() - started)


if __name__ == "__main__":
    main()
