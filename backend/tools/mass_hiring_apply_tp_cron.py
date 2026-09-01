"""Cron: real-submit to every Teleperformance (iCIMS) job once per run.

The TP analogue of the Maximus lane (`mass_hiring_apply_cron.py`), but TP is captcha-walled on every
wizard step, so it CANNOT fan out across a headless worker pool like Maximus. Instead each application
is driven by `backend.tools.icims_recon` — a headful Chromium on DISPLAY=:98 with the NopeCHA extension
solving the hCaptchas — which walks the whole iCIMS wizard (profile+state -> screeners -> EEO -> per-job
screener -> submit) and lands a real "Thank You for Applying" email in the persona's @takhet.com box.

Because that one NopeCHA browser is a shared, single resource on :98, jobs are applied to STRICTLY
SEQUENTIALLY — never in parallel (parallel runs would collide on the display + profile). Each job gets a
FRESH synthetic persona (`--fresh`) in a FRESH, isolated Chromium profile dir, so it registers a NEW
account instead of resuming a previous persona's logged-in session. Lock-guarded so overlapping runs
never stack.

    python backend/tools/mass_hiring_apply_tp_cron.py             # 1 application per TP job
    python backend/tools/mass_hiring_apply_tp_cron.py --only 1821 # just one job
    python backend/tools/mass_hiring_apply_tp_cron.py --limit 5   # first 5 TP jobs

Run under `sg mail` (icims_recon needs the mail group for mailbox provisioning + the emailed code + the
Maildir confirmation read). The subprocess inherits that group — do NOT re-wrap it in another `sg mail`.
"""
from __future__ import annotations

import argparse
import email
import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from email import policy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("tp_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db  # noqa: E402

LOCK_PATH = os.path.join(REPO, "logs", "tp_apply.lock")
MAILROOT = "/var/mail/vhosts"
MAIL_DOMAIN = "takhet.com"

# The iCIMS auto-reply that confirms a submitted application.
_TP_CONFIRM_FROM = "teleperformance+autoreply@talent.icims.com"
_TP_CONFIRM_SUBJECT_RE = re.compile(r"thank you for applying", re.I)
_PERSONA_EMAIL_RE = re.compile(r"persona:\s*.*?<([^>]+@takhet\.com)>", re.I)


def tp_job_ids() -> list[int]:
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id FROM mass_hiring_jobs WHERE apply_url ILIKE %s AND active ORDER BY id",
            ("%icims%",))
        return [r[0] for r in cur.fetchall()]


def _persona_email_from_output(out: str) -> str | None:
    """Pull the persona's @takhet.com address out of icims_recon's stdout ('persona: Name <addr> ...')."""
    m = _PERSONA_EMAIL_RE.search(out or "")
    return m.group(1).strip() if m else None


def _is_tp_confirmation(from_hdr: str, subject: str) -> bool:
    """True iff this mail is the iCIMS 'application submitted' confirmation — the From is the TP iCIMS
    auto-reply OR the Subject says 'Thank You for Applying'. Does NOT match the SHL assessment invite
    (from talentcentral@shl.com), which is a later step, not proof of submission."""
    frm = (from_hdr or "").lower()
    if _TP_CONFIRM_FROM in frm:
        return True
    return bool(_TP_CONFIRM_SUBJECT_RE.search(subject or ""))


def _mailbox_has_confirmation(localpart: str, since_ts: float) -> bool:
    """Walk the persona's Maildir (new+cur) for a TP application confirmation received at/after
    since_ts. Best-effort: any read error just means 'not found yet'."""
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
            if _is_tp_confirmation(str(msg.get("From", "")), str(msg.get("Subject", ""))):
                return True
    return False


def _nopecha_key() -> str:
    key = (os.environ.get("NOPECHA_KEY") or "").strip()
    if key:
        return key
    try:
        with open(os.path.join(REPO, "backend", ".env")) as f:
            for line in f:
                if line.startswith("NOPECHA_KEY="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def apply_one(jobid: int, keep: int) -> dict:
    """Drive ONE TP application via icims_recon in a fresh isolated profile with a fresh persona.
    Returns {jobid, persona, confirmed, error}. Never raises."""
    profile_dir = os.path.join(tempfile.gettempdir(), f"tp_prof_{jobid}_{os.getpid()}")
    shutil.rmtree(profile_dir, ignore_errors=True)
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except Exception as e:
        return {"jobid": jobid, "persona": None, "confirmed": False, "error": f"mkdir: {e}"}

    env = dict(os.environ)
    env.update({
        "ICIMS_PROFILE_DIR": profile_dir,
        "DISPLAY": ":98",
        "ICIMS_NOPECHA": "1",
        "ICIMS_PROXY": "",
        "NOPECHA_KEY": _nopecha_key(),
    })
    started = time.time()
    out = ""
    error = None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backend.tools.icims_recon",
             "--job", str(jobid), "--fresh", "--keep", str(keep)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=keep * 60 + 150)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        error = "timeout"
        # a hung Chromium keeps the profile dir open — kill anything bound to it
        try:
            subprocess.run(["pkill", "-f", profile_dir], timeout=20)
        except Exception:
            pass
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    persona = _persona_email_from_output(out)
    confirmed = False
    if persona:
        local = persona.split("@", 1)[0]
        # the confirmation email can land a minute or two after the submit click; give it a moment
        for _ in range(6):
            if _mailbox_has_confirmation(local, started):
                confirmed = True
                break
            time.sleep(10)

    shutil.rmtree(profile_dir, ignore_errors=True)
    return {"jobid": jobid, "persona": persona, "confirmed": confirmed, "error": error}


def _confirmed_jobids_in_log() -> set:
    """Jobids already logged confirmed=True in tp_apply.log (so a relaunch with --skip-confirmed can
    resume the remaining ones instead of re-applying to the ones already done this pass)."""
    ids = set()
    try:
        with open(os.path.join(REPO, "logs", "tp_apply.log")) as f:
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
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N TP jobs (0 = all)")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--keep", type=int, default=13, help="minutes cap per application")
    ap.add_argument("--rounds", type=int, default=1, help="applications per TP job this run")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent applications (each its own Chromium+NopeCHA on :98). "
                         "The box has headroom for ~3; a stuck job then only blocks 1/N throughput.")
    ap.add_argument("--skip-confirmed", action="store_true",
                    help="skip jobids already confirmed=True in tp_apply.log (resume a partial pass)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous TP apply run is still going — exiting")
        return

    if args.only:
        ids = [args.only]
    else:
        ids = tp_job_ids()
        if args.skip_confirmed:
            done = _confirmed_jobids_in_log()
            ids = [i for i in ids if i not in done]
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no Teleperformance (icims) jobs on the board")
        return

    batch = ids * max(1, args.rounds)
    workers = max(1, args.workers)
    logger.info("applying to %d TP jobs x %d round(s) = %d applications (workers=%d)",
                len(ids), args.rounds, len(batch), workers)

    results = []
    if workers > 1:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda j: _do_one(j, args.keep), batch))
    else:
        results = [_do_one(j, args.keep) for j in batch]

    submitted = sum(1 for r in results if not r.get("error"))
    confirmed = sum(1 for r in results if r.get("confirmed"))
    logger.info("tp apply run done: %d jobs, submitted=%d, confirmed=%d", len(batch), submitted, confirmed)


if __name__ == "__main__":
    main()
