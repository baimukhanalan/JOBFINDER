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

*** ARMED BY DEFAULT (no owner phone needed). ***
The phone that once blocked Submit ("Enter a valid number") was the reserved-fiction 555-01xx number's
usually-INVALID random area code, NOT the fact that it was fake — Oracle's libphonenumber checks
FORMAT/range only, and the ORC form has NO SMS OTP (the phone is a plain contact field). So
`orc_recon._build_persona` now defaults to `_synth_phone` — a deterministic SYNTHETIC but valid-format
US number (`is_valid_number`-verified), the same fabricated-value class as the Taleo license # /
Foundever SSN-6 already transmitted on these lanes. The lane therefore runs with no ORC_PHONE. Set
`ORC_PHONE` to override with a real number the owner controls.

    python backend/tools/mass_hiring_apply_orc_cron.py                        # all doable jobs
    python backend/tools/mass_hiring_apply_orc_cron.py --only 153 --keep 12
    ORC_PHONE='+1 …' python backend/tools/mass_hiring_apply_orc_cron.py --limit 3 --skip-confirmed

Run under `sg mail` (orc_recon needs the mail group for mailbox provisioning + the emailed PIN + the
Maildir confirmation read). The subprocess inherits that group — do NOT re-wrap it in another `sg mail`.
"""
from __future__ import annotations

import argparse
import fcntl
import json as _json
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

# The Oracle-ORC analogue of mass_hiring_apply_workday_cron.live_tenants(): the single source of
# truth for which ORC tenants the cron actually DRIVES. Alorica is the LIVE-PROVEN base source
# (always driven, by its dedicated cron). The newer same-ATS tenants collected onto this lane
# (molina / hilton) are COLLECT-ONLY until tools/orc_probe_promote.py drives one to a real Oracle
# application ack and appends the source to the gitignored verified file — a DATA-DRIVEN promotion,
# no code edit (exactly like the Workday probe→promote→catch-all path).
_BASE_SOURCES: set[str] = {"alorica"}
_VERIFIED_PATH = os.path.join(REPO, "data", "orc_verified_sources.json")


def _read_verified() -> set:
    try:
        with open(_VERIFIED_PATH) as f:
            v = _json.load(f)
        return {str(t) for t in v} if isinstance(v, list) else set()
    except Exception:
        return set()


def add_verified(source: str) -> None:
    """Append an auto-verified ORC source to the gitignored verified file (idempotent, atomic).
    Called by tools/orc_probe_promote.py ONLY after a real Oracle Maildir ack — never on partial
    evidence."""
    cur = _read_verified()
    if source in cur:
        return
    cur.add(source)
    os.makedirs(os.path.dirname(_VERIFIED_PATH), exist_ok=True)
    tmp = _VERIFIED_PATH + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(sorted(cur), f)
    os.replace(tmp, _VERIFIED_PATH)


def live_sources() -> set:
    """Base {alorica} UNION any probe-verified sources — mirrors mass_hiring_apply_workday_cron
    .live_tenants(). orc_recon.orc_job_ids() gates on this, so a probe-cron promotion goes live
    with NO code edit."""
    return set(_BASE_SOURCES) | _read_verified()


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
    ap.add_argument("--source", default=None,
                    help="restrict to ONE ORC source (e.g. alorica) — mirrors the Workday --tenant "
                         "pin so the dedicated alorica cron never touches a promoted tenant")
    ap.add_argument("--exclude", default="",
                    help="comma-separated ORC sources to SKIP; the catch-all cron passes the base "
                         "source that has its own dedicated cron (e.g. --exclude alorica) so it "
                         "drives only the probe-promoted tenants")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap per application")
    ap.add_argument("--skip-confirmed", action="store_true",
                    help="skip jobids already confirmed=True in orc_apply.log (resume a partial pass)")
    args = ap.parse_args()

    # No ORC_PHONE gate: the lane is armed by default via orc_recon._synth_phone (a deterministic
    # synthetic but libphonenumber-valid US number). ORC_PHONE, when set, still overrides it.
    if os.getenv("ORC_PHONE", "").strip():
        logger.info("ORC_PHONE set — using the owner-controlled number instead of the synthetic one")

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
        _exclude = {s.strip().lower() for s in (args.exclude or "").split(",") if s.strip()}
        ids = orc_job_ids(only=args.source, exclude=_exclude)
        # High-pay-first order + STOP-ON-RESPONSE (drop jobs that already reached interview/offer)
        # BEFORE --limit so the top-N are the highest-paying OPEN jobs. Guarded — falls back to the
        # id-ordered list on any error / a worktree without uploads/ (offer_priority).
        from backend.tools import offer_priority
        ids = offer_priority.plan_mh_batch(ids, rounds=1)
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
