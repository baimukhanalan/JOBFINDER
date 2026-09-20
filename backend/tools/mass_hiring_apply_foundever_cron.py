"""Cron: real-submit to every auto-applyable Foundever (SuccessFactors) job once per run.

The Foundever analogue of the TTEC/Taleo lane. Foundever's careersection has NO submit captcha, so
each application is driven HEADLESS by `backend.tools.foundever_recon`
(`FOUNDEVER_HEADLESS=1 FOUNDEVER_ADVANCE=1`) which fills the whole single-page SuccessFactors form and
lands a real "application received" email in the persona's @takhet.com box. foundever_recon isolates its
Chromium profile dir per pid, so runs can go in PARALLEL (`--workers`).

    python backend/tools/mass_hiring_apply_foundever_cron.py             # 1 application per Foundever job
    python backend/tools/mass_hiring_apply_foundever_cron.py --only 13886
    python backend/tools/mass_hiring_apply_foundever_cron.py --workers 2 --skip-confirmed

Run under `sg mail` (foundever_recon needs the mail group for mailbox provisioning + the Maildir
confirmation read). The subprocess inherits that group — do NOT re-wrap it in another `sg mail`."""
from __future__ import annotations

import argparse
import email
import fcntl
import logging
import os
import re
import subprocess
import sys
import time
from email import policy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("foundever_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

LOCK_PATH = os.path.join(REPO, "logs", "foundever_apply.lock")
MAILROOT = "/var/mail/vhosts"
MAIL_DOMAIN = "takhet.com"

_CONFIRM_SUBJECT_RE = re.compile(
    r"thank you for applying|thank you for your application|received your application|"
    r"application received|application has been received", re.I)
_PERSONA_EMAIL_RE = re.compile(r"persona:\s*.*?<([^>]+@takhet\.com)>", re.I)


def _persona_email_from_output(out: str) -> str | None:
    m = _PERSONA_EMAIL_RE.search(out or "")
    return m.group(1).strip() if m else None


def _is_confirmation(from_hdr: str, subject: str) -> bool:
    """True iff this mail is the Foundever/SuccessFactors 'application received' confirmation: from a
    successfactors/foundever/sitel sender, or a matching 'thank you for applying' subject."""
    frm = (from_hdr or "").lower()
    if any(h in frm for h in ("successfactors", "foundever", "sitel")):
        return True
    return bool(_CONFIRM_SUBJECT_RE.search(subject or ""))


def _mailbox_has_confirmation(localpart: str, since_ts: float) -> bool:
    md = os.path.join(MAILROOT, MAIL_DOMAIN, localpart)
    for sub in ("new", "cur"):
        d = os.path.join(md, sub)
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for n in names:
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < since_ts - 30:
                    continue
                with open(p, "rb") as f:
                    msg = email.message_from_binary_file(f, policy=policy.default)
            except Exception:
                continue
            if _is_confirmation(str(msg.get("From", "")), str(msg.get("Subject", ""))):
                return True
    return False


def apply_one(jobid: int, keep: int) -> dict:
    """Drive ONE Foundever application via foundever_recon (headless, fresh persona). Never raises."""
    env = dict(os.environ)
    env.update({"FOUNDEVER_HEADLESS": "1", "FOUNDEVER_ADVANCE": "1"})
    started = time.time()
    out = ""
    error = None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backend.tools.foundever_recon",
             "--job", str(jobid), "--fresh", "--keep", str(keep)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=keep * 60 + 180)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        error = "timeout"
        try:
            subprocess.run(["pkill", "-f", f"foundever_recon --job {jobid}"], timeout=20)
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    persona = _persona_email_from_output(out)
    # the on-page "Your Application has been sent" (SUBMITTED) is Foundever's own application ack; the
    # Maildir SF receipt (CONFIRMED) corroborates it. Either marker counts as a real submit.
    confirmed = "[application CONFIRMED" in out or "[application SUBMITTED" in out
    if persona and not confirmed:
        local = persona.split("@", 1)[0]
        for _ in range(6):
            if _mailbox_has_confirmation(local, started):
                confirmed = True
                break
            time.sleep(10)
    return {"jobid": jobid, "persona": persona, "confirmed": confirmed, "error": error}


def _confirmed_jobids_in_log() -> set:
    ids = set()
    try:
        with open(os.path.join(REPO, "logs", "foundever_apply.log")) as f:
            for line in f:
                m = re.search(r"applied job (\d+).*confirmed=True", line)
                if m:
                    ids.add(int(m.group(1)))
    except Exception:
        pass
    return ids


def _do_one(jobid: int, keep: int) -> dict:
    res = apply_one(jobid, keep)
    if res.get("error"):
        logger.info("applied job %s persona=%s -> ERROR %s", jobid, res.get("persona"), res["error"])
    else:
        logger.info("applied job %s persona=%s -> confirmed=%s",
                    jobid, res.get("persona"), res.get("confirmed"))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N Foundever jobs")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap per application")
    ap.add_argument("--rounds", type=int, default=1, help="applications per Foundever job this run")
    ap.add_argument("--workers", type=int, default=1, help="concurrent applications (per-pid profile)")
    ap.add_argument("--skip-confirmed", action="store_true",
                    help="skip jobids already confirmed=True in foundever_apply.log")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous Foundever apply run is still going — exiting")
        return

    if args.only:
        ids = [args.only]
    else:
        from backend.tools.foundever_recon import foundever_job_ids
        ids = foundever_job_ids()
        if args.skip_confirmed:
            done = _confirmed_jobids_in_log()
            ids = [i for i in ids if i not in done]
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no auto-applyable Foundever jobs on the board")
        return

    # High-pay-first order + STOP-ON-RESPONSE (skip jobs that already reached interview/offer) +
    # `rounds` personas/run. Guarded — falls back to `ids * rounds` on any error (offer_priority).
    from backend.tools import offer_priority
    batch = offer_priority.plan_mh_batch(ids, rounds=max(1, args.rounds))
    workers = max(1, args.workers)
    logger.info("applying to %d Foundever jobs x %d round(s) = %d applications (workers=%d, pay-ordered, open-only)",
                len(ids), args.rounds, len(batch), workers)
    if not batch:
        logger.info("every Foundever job already reached interview/offer — nothing to apply")
        return

    if workers > 1:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda j: _do_one(j, args.keep), batch))
    else:
        results = [_do_one(j, args.keep) for j in batch]

    submitted = sum(1 for r in results if not r.get("error"))
    confirmed = sum(1 for r in results if r.get("confirmed"))
    logger.info("foundever apply run done: %d jobs, submitted=%d, confirmed=%d",
                len(batch), submitted, confirmed)


if __name__ == "__main__":
    main()
