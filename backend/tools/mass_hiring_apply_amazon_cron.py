"""Cron: real-submit to every active Amazon (corporate/virtual) job once per run.

The Amazon analogue of the TTEC/Foundever/Oracle-ORC lanes. Each application is driven HEADFUL on
`:98` by `backend.tools.amazon_recon` (the Amazon Passport SPA + AWS WAF + reCAPTCHA scoring reject
headless), which creates the Passport account, verifies the emailed OTP, fills the wizard, and — when
fully armed — clicks the recorded Submit and awaits the Amazon "application received" email.

INERT UNTIL ARMED (mirrors the Oracle-ORC lane's `ORC_PHONE`-inert guard): Amazon's Passport account
creation is gated by an **AWS WAF challenge/CAPTCHA**, so this cron REFUSES to run (logs the ceiling
+ exits 0) unless AMAZON_ADVANCE=1 (the owner explicitly enabled live submission). Once it is, this
cron ARMS the lane by DEFAULT (both `os.environ.setdefault`, both overridable, both inherited by the
amazon_recon subprocess):
  * AWSWAF_BROWSER=1 — the FREE, no-key path: the page's own AWS WAF SDK mints the token for the
                       silent WAF *challenge* (aws_waf_available() → True, so the lane is no longer
                       inert). Override with AWSWAF_BROWSER=0 + CAPTCHA_SOLVER_KEY=<key> to force the
                       paid CapSolver AntiAwsWafTask (a hard visual WAF *puzzle*).
  * AMAZON_US=1     — route through us_egress (Bright Data US-pinned, from BRIGHTDATA_* in .env) so
                       the wall is served in English from a US IP (a non-flagged score). Override
                       with AMAZON_PROXY=direct (force DIRECT) or AMAZON_PROXY=<url> (an exact slot).
It is therefore safe to add to the crontab NOW: it no-ops until AMAZON_ADVANCE=1. Datacenter IPs are
risk-flagged by the WAF/reCAPTCHA score (a flagged IP is likelier to be shown the visual puzzle,
which the free path can't pass), so the BD-US egress the advance run auto-arms is what keeps the gate
the silent challenge the FREE path clears; a US-RESIDENTIAL IP would be stronger still if one exists.

    python backend/tools/mass_hiring_apply_amazon_cron.py               # 1 application per Amazon job
    python backend/tools/mass_hiring_apply_amazon_cron.py --only 9434
    python backend/tools/mass_hiring_apply_amazon_cron.py --limit 4 --workers 1

Run HEADFUL under `DISPLAY=:98 sg mail` (amazon_recon needs the mail group for mailbox provisioning +
the Maildir OTP/confirmation read; the subprocess inherits that group — do NOT re-wrap in `sg mail`).

Cron line (report-only; INERT until AMAZON_ADVANCE=1 — hour-staggered off the other lanes, minute 30
so it doesn't collide with the :00/:12/:24/:36/:42/:48/:54 lanes; one lane per phase). The advance run
auto-arms the FREE AWS-WAF path + BD-US egress, so AMAZON_ADVANCE=1 alone is enough:
  30 5 * * * cd /home/projects/jobfinder && flock -n logs/amazon_apply.lock env DISPLAY=:98 \
    AMAZON_ADVANCE=1 sg mail -c \
    'python3 -m backend.tools.mass_hiring_apply_amazon_cron --limit 4' >> logs/amazon_apply.log 2>&1
PAID fallback for a hard visual WAF puzzle — add AWSWAF_BROWSER=0 CAPTCHA_SOLVER_KEY='<key>'.
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
logger = logging.getLogger("amazon_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

LOCK_PATH = os.path.join(REPO, "logs", "amazon_apply.internal.lock")
MAILROOT = "/var/mail/vhosts"
MAIL_DOMAIN = "takhet.com"

_CONFIRM_SUBJECT_RE = re.compile(
    r"thank you for applying|thank you for your application|received your application|"
    r"application (?:has been )?received|your application to amazon", re.I)
_PERSONA_EMAIL_RE = re.compile(r"persona:\s*.*?<([^>]+@takhet\.com)>", re.I)


def _advance_enabled() -> bool:
    return os.getenv("AMAZON_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")


def _solver_armed() -> bool:
    """True when an AWS-WAF path is configured for the account-creation gate — EITHER a CapSolver
    key (visual puzzle) OR AWSWAF_BROWSER=1 (the free in-browser challenge token)."""
    try:
        from backend.applier import captcha_solver
        return captcha_solver.aws_waf_available()
    except Exception:
        return False


def _persona_email_from_output(out: str) -> str | None:
    m = _PERSONA_EMAIL_RE.search(out or "")
    return m.group(1).strip() if m else None


def _is_confirmation(from_hdr: str, subject: str) -> bool:
    frm = (from_hdr or "").lower()
    if "amazon.jobs" in frm or "@amazon.com" in frm or "hiring.amazon" in frm:
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
    """Drive ONE Amazon application via amazon_recon (headful :98, fresh persona). Never raises."""
    env = dict(os.environ)
    env.update({"AMAZON_ADVANCE": "1"})
    env.setdefault("DISPLAY", ":98")
    started = time.time()
    out = ""
    error = None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "backend.tools.amazon_recon",
             "--job", str(jobid), "--fresh", "--keep", str(keep)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=keep * 60 + 240)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        error = "timeout"
        try:
            subprocess.run(["pkill", "-f", f"amazon_recon --job {jobid}"], timeout=20)
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    persona = _persona_email_from_output(out)
    confirmed = "[application CONFIRMED" in out
    if persona and not confirmed:
        local = persona.split("@", 1)[0]
        for _ in range(6):
            if _mailbox_has_confirmation(local, started):
                confirmed = True
                break
            time.sleep(10)
    return {"jobid": jobid, "persona": persona, "confirmed": confirmed, "error": error}


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
    ap.add_argument("--limit", type=int, default=0, help="apply to only the first N Amazon jobs")
    ap.add_argument("--only", type=int, default=0, help="apply to just this one mass_hiring_jobs id")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap per application")
    ap.add_argument("--rounds", type=int, default=1, help="applications per Amazon job this run")
    ap.add_argument("--workers", type=int, default=1, help="concurrent applications (per-pid profile)")
    args = ap.parse_args()

    # INERT until armed: without AMAZON_ADVANCE the account is never created, so a live run would
    # only burn attempts on the Passport wall. Exit 0 (safe in crontab).
    if not _advance_enabled():
        logger.info("AMAZON_ADVANCE not set — Amazon apply lane INERT (dry-run only via amazon_recon). "
                    "Exiting without applying.")
        return
    # ARM the FREE AWS-WAF path + Bright Data US egress by DEFAULT once the owner set AMAZON_ADVANCE
    # (both overridable, both inherited by the amazon_recon subprocess via os.environ):
    #   * AWSWAF_BROWSER=1 — the page's own AwsWafIntegration.getToken() clears the silent WAF
    #     *challenge* with no key, so aws_waf_available() is True (the lane is no longer inert).
    #     Set AWSWAF_BROWSER=0 + CAPTCHA_SOLVER_KEY=<key> to force the paid visual-puzzle path.
    #   * AMAZON_US=1 — route through us_egress (Bright Data US-pinned, from BRIGHTDATA_* in .env) so
    #     the wall is served in English from a US IP (a non-flagged score). AMAZON_PROXY=direct forces
    #     DIRECT; AMAZON_PROXY=<url> pins an exact US slot.
    os.environ.setdefault("AWSWAF_BROWSER", "1")
    os.environ.setdefault("AMAZON_US", "1")
    if not _solver_armed():
        logger.info("No AWS WAF path armed (AWSWAF_BROWSER=0 with no CAPTCHA_SOLVER_KEY) — the Amazon "
                    "Passport account cannot be created (NopeCHA does NOT solve AWS WAF). "
                    "Lane INERT. Exiting.")
        return

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous Amazon apply run is still going — exiting")
        return

    if args.only:
        ids = [args.only]
    else:
        from backend.tools.amazon_recon import amazon_job_ids
        ids = amazon_job_ids()
        if args.limit and args.limit > 0:
            ids = ids[:args.limit]
    if not ids:
        logger.info("no active Amazon jobs on the board")
        return

    # High-pay-first + STOP-ON-RESPONSE + `rounds` personas/run (guarded; falls back to ids*rounds).
    from backend.tools import offer_priority
    batch = offer_priority.plan_mh_batch(ids, rounds=max(1, args.rounds))
    workers = max(1, args.workers)
    logger.info("applying to %d Amazon jobs x %d round(s) = %d applications (workers=%d, headful :98)",
                len(ids), args.rounds, len(batch), workers)
    if not batch:
        logger.info("every Amazon job already reached interview/offer — nothing to apply")
        return

    if workers > 1:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda j: _do_one(j, args.keep), batch))
    else:
        results = [_do_one(j, args.keep) for j in batch]

    submitted = sum(1 for r in results if not r.get("error"))
    confirmed = sum(1 for r in results if r.get("confirmed"))
    logger.info("amazon apply run done: %d jobs, no-error=%d, confirmed=%d",
                len(batch), submitted, confirmed)


if __name__ == "__main__":
    main()
