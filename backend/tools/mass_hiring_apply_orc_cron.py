"""Cron: real-submit to every auto-applyable Oracle Recruiting Cloud (ORC) / Alorica job once per run.

The Oracle-CX analogue of the Taleo / Maximus lanes. Oracle CX guest apply is LOGIN-LESS and has NO
interactive captcha — the only anti-bot is (a) an emailed verification PIN (machine-readable from the
persona's @takhet.com Maildir, like the GH/Ashby security code) and (b) an INVISIBLE reCAPTCHA v3.
Each application is driven by `backend.tools.orc_recon` (`ORC_ADVANCE=1`), which clicks Apply, accepts
the guest Terms dialog, walks the Redwood single-page form (Title + address comboboxes + Yes/No
screeners + EEO decline + WOTC), then clicks Submit + fills the emailed PIN and awaits the Oracle
"application received" receipt in the persona's Maildir.

Oracle scores an INVISIBLE reCAPTCHA v3, so the fill is driven HEADFUL on the shared display (`:98`,
like the Workday/Maximus lanes), not headless — set `ORC_HEADLESS=1` to override.

*** GATED on ORC_PHONE (owner policy). ***
synth_persona mints a reserved-fiction 555-01xx phone so a persona can never be submitted as a real
person; Oracle's libphonenumber rejects it ("Enter a valid number") and blocks Submit. A number can't
be both guaranteed-fake AND format-valid, so a real ACK requires a VALID US number the owner controls.
This lane therefore REFUSES to run (logs + exits 0) unless ORC_PHONE is set to such a number — so it is
safe to wire into cron NOW and stays inert (no wasted attempts) until the owner opts in.

    ORC_PHONE='+1 216 555 0135' python backend/tools/mass_hiring_apply_orc_cron.py            # all doable jobs
    ORC_PHONE='+1 …' python backend/tools/mass_hiring_apply_orc_cron.py --only 153 --keep 12
    ORC_PHONE='+1 …' python backend/tools/mass_hiring_apply_orc_cron.py --limit 3 --skip-confirmed

Run under `sg mail` (orc_recon needs the mail group for mailbox provisioning + the emailed PIN + the
Maildir confirmation read). The subprocess inherits that group — do NOT re-wrap it in another `sg mail`.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import re
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("orc_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

LOCK_PATH = os.path.join(REPO, "logs", "orc_apply.lock")
MAILROOT = "/var/mail/vhosts/takhet.com"
_PERSONA_EMAIL_RE = re.compile(r"persona:\s*.*?<([^>]+@takhet\.com)>", re.I)


def _persona_email_from_output(out: str) -> str | None:
    m = _PERSONA_EMAIL_RE.search(out or "")
    return m.group(1).strip() if m else None


def apply_one(jobid: int, keep: int) -> dict:
    """Drive ONE Oracle ORC application via orc_recon (headful on :98, fresh persona). Returns
    {jobid, persona, confirmed, error}. Never raises. ORC_PHONE is inherited from the environment."""
    from backend.tools.orc_recon import _app_confirmed
    env = dict(os.environ)
    env["ORC_ADVANCE"] = "1"
    started = time.time()
    out = ""
    error = None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backend.tools.orc_recon",
             "--job", str(jobid), "--fresh", "--keep", str(keep)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=keep * 60 + 150)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        error = "timeout"
        try:
            subprocess.run(["pkill", "-f", f"orc_recon --job {jobid}"], timeout=20)
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    persona = _persona_email_from_output(out)
    confirmed = False
    if persona:
        # the Oracle receipt can land a minute or two after submit; give it a moment.
        for _ in range(6):
            if _app_confirmed(persona, started - 60):
                confirmed = True
                break
            time.sleep(10)
    return {"jobid": jobid, "persona": persona, "confirmed": confirmed, "error": error}


def _confirmed_jobids_in_log() -> set:
    ids = set()
    try:
        with open(os.path.join(REPO, "logs", "orc_apply.log")) as f:
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
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N ORC jobs (0 = all)")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap per application")
    ap.add_argument("--skip-confirmed", action="store_true",
                    help="skip jobids already confirmed=True in orc_apply.log (resume a partial pass)")
    args = ap.parse_args()

    # OWNER-POLICY GATE: no valid phone → no possible ack (555-01xx is rejected by Oracle). Stay inert.
    if not os.getenv("ORC_PHONE", "").strip():
        logger.info("ORC_PHONE not set — the reserved-fiction 555-01xx persona phone fails Oracle's "
                    "phone validation, so no application can be accepted. Lane INERT (set ORC_PHONE to "
                    "a VALID US number the owner controls to enable). Exiting.")
        return

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous ORC apply run is still going — exiting")
        return

    from backend.tools.orc_recon import orc_job_ids
    if args.only:
        ids = [args.only]
    else:
        ids = orc_job_ids()
        if args.skip_confirmed:
            done = _confirmed_jobids_in_log()
            ids = [i for i in ids if i not in done]
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no auto-applyable Oracle ORC (Alorica) jobs on the board")
        return

    # Sequential only: the fill is HEADFUL on the single shared display (:98) — parallel headful
    # browsers fight the one display + inflate the reCAPTCHA-v3 risk score.
    logger.info("applying to %d Oracle ORC jobs (sequential, headful :98)", len(ids))
    results = [_do_one(j, args.keep) for j in ids]

    submitted = sum(1 for r in results if not r.get("error"))
    confirmed = sum(1 for r in results if r.get("confirmed"))
    logger.info("orc apply run done: %d jobs, submitted=%d, confirmed=%d",
                len(ids), submitted, confirmed)


if __name__ == "__main__":
    main()
