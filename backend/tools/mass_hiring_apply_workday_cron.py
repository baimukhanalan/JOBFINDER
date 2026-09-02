"""Cron: real-submit to the validated Workday CxS mass-hiring tenants once per run.

The Workday analogue of the Maximus lane (`mass_hiring_apply_cron.py`). Drives the Workday
CxS tenants through the shared `run_batch_parallel(dry_run=False)` path — fresh headless
bulk_pool workers with `WORKDAY_ADVANCE=1`, one guest candidate account per job (email +
generated password), the CxS wizard filled, and the final Submit clicked. Workday emails the
persona an "application received / under review" ack (ground truth, read from the Maildir).

Tenants: only the ones LIVE-VALIDATED to produce a real ack are driven (see `_LIVE_TENANTS`).
A tenant blocked at the register step (reCAPTCHA needing a solver key + residential IP) or by
Workday's activation-email throttle is listed in `_BLOCKED` with the reason and NOT driven.

Paced CONSERVATIVELY (2x/day, not 5x): every Workday tenant sends its account + ack mail from
one sender domain (*.workday.com), and Workday throttles activation mail to a recipient domain
after a burst — so we cap accounts/day to stay under it.

    python backend/tools/mass_hiring_apply_workday_cron.py                 # all live tenants, 1 app each
    python backend/tools/mass_hiring_apply_workday_cron.py --tenant cigna  # just one tenant
    python backend/tools/mass_hiring_apply_workday_cron.py --workers 4

Run under `sg mail` (mailbox provisioning + the Maildir ack read). The bulk_pool workers are
headless, so no display is needed.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import urllib.parse as _up

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

# Tenants live-validated to land a real Workday application ack. EMPTY on purpose: a live
# 4-tenant smoke (2026-09-02) drove one sacrificial job each and got 0 acks — see _BLOCKED.
# So this cron is a SAFE NO-OP (drives nothing) until a tenant is freshly validated.
#
# NB Centene (centene.wd5) is the one Workday tenant proven to complete KEYLESS end-to-end —
# 18 real "Your Centene application is under review" acks in mail_index — because its register
# step has no blocking reCAPTCHA. It is NOT listed here only because it was not part of this
# run's fresh validation; add "centene" after one confirmed run to enable a paced Centene lane.
_LIVE_TENANTS: set[str] = set()

# Tenants NOT driven, with the wall (2026-09-02 smoke: all four returned filled=0, no account
# created, no mail — the co-pilot never cleared the guest-account CREATE gate). The gate is the
# register-step reCAPTCHA, a no-op here because no CAPTCHA_SOLVER_KEY (capsolver/2captcha) is
# configured; a datacenter IP would also be risk-scored, so a US residential IP is wanted too.
# The confirming captcha-presence log was inconclusive per tenant, so both candidate walls are
# cited (reCAPTCHA solver + the ~90-account/day *.workday.com activation-email throttle).
_BLOCKED: dict[str, str] = {
    "cigna": "account-create gate not cleared keyless (filled=0, no verify mail) — register "
             "reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP; may compound with the "
             "~90-account/day *.workday.com activation-email throttle",
    "humana": "account-create gate not cleared keyless (filled=0, no verify mail) — register "
              "reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP",
    "cvshealth": "account-create gate not cleared keyless (filled=0, no verify mail) — register "
                 "reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP",
    "concentrix": "account-create gate not cleared keyless (filled=0, no verify mail) — register "
                  "reCAPTCHA needs CAPTCHA_SOLVER_KEY + a US residential IP",
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
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=1, help="applications per job this run")
    ap.add_argument("--tenant", default=None, help="restrict to one tenant (e.g. cigna)")
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
    if not ids:
        logger.info("no Workday jobs to apply to (tenants=%s)", sorted(_LIVE_TENANTS))
        return
    batch = ids * max(1, args.rounds)
    logger.info("applying to %d Workday jobs x %d round(s) = %d (tenants=%s)",
                len(ids), args.rounds, len(batch), sorted(_LIVE_TENANTS))
    res = mha.run_batch_parallel(batch, workers=args.workers, gender=None,
                                 dry_run=False, per_job_timeout=520)
    conf = sum(1 for r in res if r.get("confirmed"))
    clicked = sum(1 for r in res if r.get("clicked"))
    logger.info("Workday apply run done: %d jobs, clicked=%d, confirmed=%d", len(res), clicked, conf)


if __name__ == "__main__":
    main()
