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
import json as _json
import logging
import os
import sys
import urllib.parse as _up

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("mh_apply_cron")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from backend.tools import mail_db, mass_hiring_apply as mha, offer_priority  # noqa: E402

LOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "mh_apply_cron.lock")

# ---- live tenant gate (Maximus base UNION auto-verified Avature tenants) -------------------------
# The Avature lane is keyed by the apply_url HOST. Maximus posts on `*.avature.net`; Transcom is ALSO
# Avature but its apply_url host is `apply.careers.transcom.com` (transcom.avature.net 302s to it).
# Maximus (the proven tenant) is ALWAYS driven; Transcom is a DIFFERENT Avature tenant whose screener
# set MAY differ from Maximus's `_SCREENERS`, so it is driven ONLY once the self-verify probe
# (tools/avature_probe_promote.py) has landed a real account-created/submit ack and appended it
# (gitignored, data-driven — no code edit) to data/avature_verified_sources.json. This keeps the
# Maximus selection BYTE-IDENTICAL while gating Transcom behind a live verification.
_BASE_AVATURE_SOURCES = frozenset({"maximus"})
_VERIFIED_SOURCES_PATH = os.path.join(REPO, "data", "avature_verified_sources.json")


def _read_verified_sources() -> set:
    try:
        with open(_VERIFIED_SOURCES_PATH) as f:
            v = _json.load(f)
        return {str(s) for s in v} if isinstance(v, list) else set()
    except Exception:
        return set()


def add_verified_source(source: str) -> None:
    """Append an auto-verified Avature tenant SOURCE to the gitignored file (idempotent, atomic).
    Called by avature_probe_promote on a confirmed ack — live_sources() then unions it in."""
    cur = _read_verified_sources()
    if source in cur:
        return
    cur.add(source)
    os.makedirs(os.path.dirname(_VERIFIED_SOURCES_PATH), exist_ok=True)
    tmp = _VERIFIED_SOURCES_PATH + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(sorted(cur), f)
    os.replace(tmp, _VERIFIED_SOURCES_PATH)


def live_sources() -> set:
    """Base Maximus source UNION any auto-verified Avature tenant sources — the single source of
    truth for which Avature tenants the cron drives. Probe promotion writes the file; no code edit."""
    return set(_BASE_AVATURE_SOURCES) | _read_verified_sources()


def _avature_tenant(url: str) -> str:
    """Map an apply_url to its Avature tenant key. The Transcom host is its own tenant; EVERY
    `*.avature.net` host is the Maximus base lane (so Maximus stays byte-identical, keyed on the
    HOST not the `source` column — no non-Maximus avature.net tenant exists today, but this never
    silently drops a Maximus job if one appeared)."""
    host = _up.urlparse((url or "").lower()).netloc
    if "apply.careers.transcom.com" in host:
        return "transcom"
    return "maximus"


def maximus_ids(only: str | None = None, exclude: set[str] | None = None) -> list[int]:
    """Active Avature jobs whose tenant is live-validated. Maximus is always driven; Transcom only
    once verified. `only` restricts to one tenant; `exclude` drops the named tenants — the catch-all
    cron passes `exclude={'maximus'}` so it drives only the AUTO-PROMOTED tenants (Maximus runs on
    its own dedicated cron), mirroring the Workday `workday_ids(only=, exclude=)` catch-all pattern.

    `active` guard mirrors every other lane (Kelly/SR/Taleo/TP/Workday) — without it the lane applied
    to DELISTED jobs, minting a fresh persona+mailbox per dead id (~50 wasted/day)."""
    live = live_sources()
    exclude = exclude or set()
    with mail_db.conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id, apply_url FROM mass_hiring_jobs WHERE active "
            "AND (apply_url ILIKE %s OR apply_url ILIKE %s) ORDER BY id",
            ("%avature%", "%apply.careers.transcom.com%"))
        rows = cur.fetchall()
    out: list[int] = []
    for jid, url in rows:
        tenant = _avature_tenant(url)
        if tenant not in live:
            continue
        if only and tenant != only:
            continue
        if tenant in exclude:
            continue
        out.append(jid)
    from backend.tools import mh_settings
    return mh_settings.drop_spanish(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=1, help="applications per Maximus job this run")
    ap.add_argument("--source", default=None,
                    help="restrict to one Avature tenant (e.g. transcom)")
    ap.add_argument("--exclude", default="",
                    help="comma-separated Avature tenants to SKIP (the catch-all cron passes the base "
                         "'maximus' that has its own dedicated cron line)")
    args = ap.parse_args()
    _exclude = {t.strip().lower() for t in (args.exclude or "").split(",") if t.strip()}

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("a previous apply run is still going — exiting")
        return

    ids = maximus_ids(only=args.source, exclude=_exclude)
    if not ids:
        logger.info("no Maximus (avature) jobs on the board")
        return
    # High-pay-first order + STOP-ON-RESPONSE (skip jobs that already reached interview/offer) +
    # `rounds` personas/run. Guarded — falls back to `ids * rounds` on any error (offer_priority).
    batch = offer_priority.plan_mh_batch(ids, rounds=max(1, args.rounds))
    logger.info("applying to %d Maximus jobs x %d round(s) = %d applications (pay-ordered, open-only)",
                len(ids), args.rounds, len(batch))
    if not batch:
        logger.info("every Maximus job already reached interview/offer — nothing to apply")
        return
    res = mha.run_batch_parallel(batch, workers=args.workers, gender=None,
                                 dry_run=False, per_job_timeout=420)
    conf = sum(1 for r in res if r.get("confirmed"))
    clicked = sum(1 for r in res if r.get("clicked"))
    logger.info("apply run done: %d jobs, clicked=%d, confirmed=%d", len(res), clicked, conf)


if __name__ == "__main__":
    main()
