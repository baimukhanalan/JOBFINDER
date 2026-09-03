"""Cron: real-submit to the validated Workday CxS mass-hiring tenants once per run.

The Workday analogue of the Maximus/TP lanes. Drives each live-validated Workday CxS job
through `workday_recon.drive_apply(advance_env="WORKDAY_ADVANCE")` — a HEADFUL persistent-context
Chromium on DISPLAY=:98 that clicks Apply → creates the guest account (no register captcha on the
validated tenants) → confirms the emailed activation link → fills every wizard step → clicks the
final Submit → then polls the persona's @takhet.com Maildir for the real "application under review"
receipt (ground truth). One fresh synthetic persona + guest account per job.

Why HEADFUL (not the headless bulk_pool `run_batch_parallel`): Workday CxS treats a plain headless
Chromium differently — the Apply → guest-apply flow bounces straight back to the job posting
(verified 2026-09-03: two headless attempts on Centene stalled at filled=0/job_listing, while the
same job under the headful recon reached account created=True and a real "Your Centene application
is under review" ack from centene@myworkday.com). So this lane runs the headful recon the same way
the TP (iCIMS) lane runs `icims_recon`.

Tenants: only the ones LIVE-VALIDATED to produce a real ack are driven (see `_LIVE_TENANTS`).
A tenant blocked at the register step (reCAPTCHA needing a solver key + residential IP) is listed
in `_BLOCKED` with the reason and NOT driven.

    python -m backend.tools.mass_hiring_apply_workday_cron                    # all live tenants
    python -m backend.tools.mass_hiring_apply_workday_cron --tenant centene   # just one tenant
    python -m backend.tools.mass_hiring_apply_workday_cron --workers 2 --limit 1

Run under `sg mail` (mailbox provisioning + the activation-link + Maildir ack read) AND with
DISPLAY=:98 (the recon is headful). WORKDAY_ADVANCE=1 must be set so the wizard walks past the
account gate and the final Submit is clicked — the crontab line sets it.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import urllib.parse as _up
from concurrent.futures import ThreadPoolExecutor

# The headful recon walks the account-create + wizard + final Submit; the class-level
# `advance_wizard` gate on WorkdayMassHiringStrategy is read when the strategy module is first
# imported (at drive time in workday_recon). Set it here BEFORE that import so a direct invocation
# (without the crontab env) still advances; the crontab line also exports it.
os.environ.setdefault("WORKDAY_ADVANCE", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("workday_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db, mass_hiring_apply as mha  # noqa: E402

LOCK_PATH = os.path.join(REPO, "logs", "workday_apply.lock")

# host-slug -> friendly tenant name
_TENANT = {"cnx": "concentrix", "cvshealth": "cvshealth", "centene": "centene",
           "cigna": "cigna", "humana": "humana"}

# Tenants live-validated to land a real Workday application ack — driven by the cron.
#
# Centene (centene.wd5) is proven KEYLESS end-to-end: its register step carries NO reCAPTCHA
# (grecaptcha False / sitekey None on the live create-account page), so the headful recon creates
# the guest account, confirms the emailed activation link, fills the 7-step wizard, and submits —
# landing a real "Your Centene application is under review" receipt from centene@myworkday.com in
# the persona's @takhet.com Maildir (validated 2026-09-03, fresh synthetic persona).
_LIVE_TENANTS: set[str] = {"centene"}

# Tenants NOT driven. The insurer/BPO tenants gate the account-create step behind a reCAPTCHA that
# needs a CAPTCHA_SOLVER_KEY (capsolver/2captcha) AND a US residential IP (a datacenter IP is
# risk-scored on the register step), so they cannot complete keyless from here. Do NOT add them
# without a solver key + residential egress AND a fresh live-validated ack.
_BLOCKED: dict[str, str] = {
    "cigna": "register-step reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP (not keyless)",
    "humana": "register-step reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP (not keyless)",
    "cvshealth": "register-step reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP (not keyless)",
    "concentrix": "register-step reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP (not keyless)",
}


def _tenant_of(url: str) -> str:
    host = _up.urlparse((url or "").lower()).netloc
    slug = host.split(".")[0]
    return _TENANT.get(slug, slug)


def workday_ids(only: str | None = None) -> list[int]:
    """Active Workday-tenant jobs that are (a) in SUPPORTED_HOSTS and (b) a live-validated tenant."""
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT id, apply_url FROM mass_hiring_jobs "
                    "WHERE active AND apply_url ILIKE %s ORDER BY id", ("%myworkdayjobs.com%",))
        rows = cur.fetchall()
    out: list[int] = []
    for jid, url in rows:
        if not mha.is_supported(url):
            continue
        tenant = _tenant_of(url)
        if only and tenant != only:
            continue
        if tenant not in _LIVE_TENANTS:
            continue
        out.append(jid)
    from backend.tools import mh_settings
    return mh_settings.drop_spanish(out)


def _drive_job(jid: int, keep: int) -> dict:
    """Drive ONE Workday job to a real ack via the headful recon (own event loop per thread, so
    concurrent headful browsers coexist on :98 like the TP lane). Never raises."""
    import asyncio

    from backend.tools import workday_recon
    row = workday_recon._row(jid)
    if not row:
        return {"id": jid, "error": "no row"}
    try:
        res = asyncio.run(workday_recon.drive_apply(
            row, advance_env="WORKDAY_ADVANCE", keep_minutes=keep))
        res["id"] = jid
        return res
    except Exception as e:  # noqa: BLE001 — a per-job failure must not sink the pass
        return {"id": jid, "error": f"{type(e).__name__}: {e}"[:200]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent headful applications on :98 (1 = main-thread sequential, the "
                         "proven path; >1 runs that many headful browsers at once)")
    ap.add_argument("--keep", type=int, default=12, help="minutes cap per application (Maildir ack poll)")
    ap.add_argument("--tenant", default=None, help="restrict to one tenant (e.g. centene)")
    ap.add_argument("--limit", type=int, default=0, help="apply to at most N jobs this run (0 = all)")
    args = ap.parse_args()

    if not _LIVE_TENANTS:
        logger.info("no live-validated Workday tenants configured — nothing to drive")
        return

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous Workday apply run is still going — exiting")
        return

    ids = workday_ids(only=args.tenant)
    if args.limit and args.limit > 0:
        ids = ids[:args.limit]
    if not ids:
        logger.info("no Workday jobs to apply to (tenants=%s)", sorted(_LIVE_TENANTS))
        return
    workers = max(1, min(int(args.workers), len(ids)))
    logger.info("applying to %d Workday job(s) headful on :98 (tenants=%s, workers=%d, keep=%dm)",
                len(ids), sorted(_LIVE_TENANTS), workers, args.keep)

    results: list[dict] = []

    def _record(res: dict) -> None:
        results.append(res)
        logger.info("job %s persona=%s clicked=%s confirmed=%s error=%s",
                    res.get("id"), res.get("persona"), res.get("clicked"),
                    res.get("confirmed"), res.get("error"))

    if workers == 1:
        # Main-thread sequential — byte-identical to the proven workday_recon.main invocation
        # (asyncio.run per job in the main thread). Preferred for the live lane: no threaded
        # event loop, one headful browser at a time on :98, kindest to display contention.
        for jid in ids:
            _record(_drive_job(jid, args.keep))
    else:
        # Concurrent headful browsers on :98 (each its own event loop + browser), like the TP lane.
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(lambda j: _drive_job(j, args.keep), ids):
                _record(res)

    conf = sum(1 for r in results if r.get("confirmed"))
    clicked = sum(1 for r in results if r.get("clicked"))
    logger.info("Workday apply run done: %d jobs, clicked=%d, confirmed=%d", len(results), clicked, conf)


if __name__ == "__main__":
    main()
