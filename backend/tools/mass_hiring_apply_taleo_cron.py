"""Cron: real-submit to every auto-applyable TTEC (Oracle Taleo) job once per run.

The Taleo analogue of the TP / Maximus lanes. Unlike TP (hCaptcha-walled → NopeCHA headful), Taleo has
NO submit captcha, so each application is driven HEADLESS by `backend.tools.taleo_recon`
(`TALEO_HEADLESS=1 TALEO_ADVANCE=1`) which walks the whole JSF wizard (profile + résumé + emailed code →
screeners → EEO → per-job screener → submit) and lands a real "Thank you for applying to TTEC" email in
the persona's @takhet.com box. taleo_recon isolates its Chromium profile dir per pid, so runs can go in
PARALLEL (`--workers`) — each a fresh headless browser, no shared display.

Doable set (`ttec_job_ids`): source='ttec', active, EXCLUDING (a) licensed-insurance roles (a synthetic
persona holds no real license) and (b) exotic-bilingual roles requiring a language we cannot honestly
staff — only English-only and Spanish/Russian-bilingual roles qualify (the Russian-English Bilingual
Healthcare CSR is staffed by an English+Russian persona: a truthful persona-DESIGN attribute, and the
downstream Russian language test is passed by the team). UnitedHealth (Azure-AD SSO, no self-register)
is deliberately NOT in this set.

    python backend/tools/mass_hiring_apply_taleo_cron.py             # 1 application per doable TTEC job
    python backend/tools/mass_hiring_apply_taleo_cron.py --only 518  # just the Russian-bilingual one
    python backend/tools/mass_hiring_apply_taleo_cron.py --workers 3 --skip-confirmed

Run under `sg mail` (taleo_recon needs the mail group for mailbox provisioning + the emailed code + the
Maildir confirmation read). The subprocess inherits that group — do NOT re-wrap it in another `sg mail`.
"""
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
logger = logging.getLogger("taleo_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db  # noqa: E402

LOCK_PATH = os.path.join(REPO, "logs", "taleo_apply.lock")
MAILROOT = "/var/mail/vhosts"
MAIL_DOMAIN = "takhet.com"

# The TTEC talent-acquisition auto-reply that confirms a submitted application (all confirmed acks —
# 'Thank you for applying to TTEC …', 'Thank you for applying with TTEC for …', 'Your Application -
# Required Assessments' — come From this address). NOT the later SHL/AMCAT assessment invite.
_TTEC_CONFIRM_FROM = "jobopportunities@ttec.com"
_TTEC_CONFIRM_SUBJECT_RE = re.compile(r"thank you for applying|required assessment", re.I)
_PERSONA_EMAIL_RE = re.compile(r"persona:\s*.*?<([^>]+@takhet\.com)>", re.I)


def ttec_job_ids() -> list[int]:
    """Active TTEC (Taleo) jobs we can honestly staff — excludes licensed-insurance + exotic-bilingual
    (a non-English/Spanish/Russian language). UnitedHealth is excluded (SSO)."""
    from backend.tools.synth_persona import job_is_staffable
    from backend.tools.taleo_recon import _TTEC_LICENSED_IDS, is_licensed
    out: list[int] = []
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, title FROM mass_hiring_jobs WHERE source='ttec' AND active ORDER BY id")
        for jid, title in cur.fetchall():
            if jid in _TTEC_LICENSED_IDS or is_licensed(title):
                continue
            if not job_is_staffable({"title": title}):
                continue
            out.append(jid)
    from backend.tools import mh_settings
    return mh_settings.drop_spanish(out)


def _persona_email_from_output(out: str) -> str | None:
    """Pull the persona's @takhet.com address out of taleo_recon's stdout ('persona: Name <addr> …')."""
    m = _PERSONA_EMAIL_RE.search(out or "")
    return m.group(1).strip() if m else None


def _is_ttec_confirmation(from_hdr: str, subject: str) -> bool:
    """True iff this mail is the TTEC 'application submitted' confirmation: From the TTEC talent-
    acquisition address, or a Subject that says 'Thank you for applying' / 'Required Assessments'."""
    frm = (from_hdr or "").lower()
    if _TTEC_CONFIRM_FROM in frm:
        return True
    return bool(_TTEC_CONFIRM_SUBJECT_RE.search(subject or ""))


def _mailbox_has_confirmation(localpart: str, since_ts: float) -> bool:
    """Walk the persona's Maildir (new+cur) for a TTEC confirmation received at/after since_ts."""
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
            if _is_ttec_confirmation(str(msg.get("From", "")), str(msg.get("Subject", ""))):
                return True
    return False


def apply_one(jobid: int, keep: int) -> dict:
    """Drive ONE TTEC application via taleo_recon (headless, fresh persona). Returns
    {jobid, persona, confirmed, error}. Never raises."""
    env = dict(os.environ)
    env.update({"TALEO_HEADLESS": "1", "TALEO_ADVANCE": "1"})
    started = time.time()
    out = ""
    error = None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backend.tools.taleo_recon",
             "--job", str(jobid), "--fresh", "--keep", str(keep)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=keep * 60 + 150)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        error = "timeout"
        try:
            subprocess.run(["pkill", "-f", f"taleo_recon --job {jobid}"], timeout=20)
        except Exception:
            pass
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    persona = _persona_email_from_output(out)
    confirmed = False
    if persona:
        local = persona.split("@", 1)[0]
        # the TTEC confirmation can land a minute or two after submit; give it a moment
        for _ in range(6):
            if _mailbox_has_confirmation(local, started):
                confirmed = True
                break
            time.sleep(10)
    return {"jobid": jobid, "persona": persona, "confirmed": confirmed, "error": error}


def _confirmed_jobids_in_log() -> set:
    """Jobids already logged confirmed=True in taleo_apply.log (for --skip-confirmed resume)."""
    ids = set()
    try:
        with open(os.path.join(REPO, "logs", "taleo_apply.log")) as f:
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
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N TTEC jobs (0 = all)")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap per application")
    ap.add_argument("--rounds", type=int, default=1, help="applications per TTEC job this run")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent applications (each its own headless Chromium; per-pid profile dir, "
                         "so parallel is safe). The box has headroom for ~3.")
    ap.add_argument("--skip-confirmed", action="store_true",
                    help="skip jobids already confirmed=True in taleo_apply.log (resume a partial pass)")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous TTEC apply run is still going — exiting")
        return

    if args.only:
        ids = [args.only]
    else:
        ids = ttec_job_ids()
        if args.skip_confirmed:
            done = _confirmed_jobids_in_log()
            ids = [i for i in ids if i not in done]
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no auto-applyable TTEC (Taleo) jobs on the board")
        return

    batch = ids * max(1, args.rounds)
    workers = max(1, args.workers)
    logger.info("applying to %d TTEC jobs x %d round(s) = %d applications (workers=%d)",
                len(ids), args.rounds, len(batch), workers)

    if workers > 1:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda j: _do_one(j, args.keep), batch))
    else:
        results = [_do_one(j, args.keep) for j in batch]

    submitted = sum(1 for r in results if not r.get("error"))
    confirmed = sum(1 for r in results if r.get("confirmed"))
    logger.info("taleo apply run done: %d jobs, submitted=%d, confirmed=%d",
                len(batch), submitted, confirmed)


if __name__ == "__main__":
    main()
