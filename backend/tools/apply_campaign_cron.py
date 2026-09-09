"""Recurring apply-campaign driver (cron).

For each ACTIVE campaign, apply its per-day budget of target jobs under the campaign's fixed custom
name + stable mailbox, SEQUENTIALLY through the single co-pilot (reuses dashboard_app._do_fill, which
blocks on the co-pilot /load and auto-submits), then records the run. fcntl-locked so overlapping
cron starts collapse to one. Sequential-via-the-single-co-pilot on purpose — it sidesteps the shared
bulk_pool worker-port race with a manual «Подать на все».

Run under DISPLAY=:98 + sg mail (the co-pilot fill needs the headful display + mailbox provisioning).
    python -m backend.tools.apply_campaign_cron            # apply all active campaigns' daily budget
    python -m backend.tools.apply_campaign_cron --list     # show campaigns, apply nothing
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import logging
import os


def _today() -> str:
    return datetime.date.today().isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show campaigns + planned targets, apply nothing")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("apply_campaigns")

    from backend.tools import apply_campaigns
    today = _today()
    camps = apply_campaigns.list_campaigns()
    active = [c for c in camps if c.get("active")]

    if args.list:
        for c in camps:
            kind = c.get("target_kind")
            if kind == "job":
                target = c.get("job_id")
            elif kind == "jobs":
                target = f"jobs={len(c.get('job_ids') or [])} cursor={c.get('cursor', 0)}"
            else:
                target = f"q={c.get('q')!r} region={c.get('region')!r}"
            log.info("campaign %s %r kind=%s per_day=%s active=%s runs_today=%s target=%s",
                     c.get("id"), c.get("name"), kind, c.get("per_day"),
                     c.get("active"), c.get("runs_today"), target)
        return

    lock = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "apply_campaign_cron.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    lf = open(lock, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("another apply-campaign run is going — exiting")
        return

    log.info("apply-campaigns: %d active of %d", len(active), len(camps))
    # Import here (after the lock) so the dashboard's background threads only spin up for a real run.
    from backend.dashboard_app import _do_fill, _FILL_JOBS

    total = 0
    for c in active:
        try:
            targets = apply_campaigns.resolve_targets(c, today)
        except Exception as exc:
            log.info("campaign %s resolve failed: %s", c.get("id"), exc)
            continue
        if not targets:
            log.info("campaign %s (%s): nothing to apply today (per_day=%s runs_today=%s)",
                     c.get("id"), c.get("name"), c.get("per_day"), c.get("runs_today"))
            continue
        done = []
        for jid in targets:
            try:
                _do_fill(int(jid), c.get("gender") or None, c.get("name"),
                         c.get("email"), c.get("pid"))
                st = _FILL_JOBS.get(int(jid), {}) or {}
                log.info("campaign %s job %s -> %s%s", c.get("id"), jid, st.get("state"),
                         (" submit=" + str((st.get("submit") or {}).get("reason") or
                                           (st.get("submit") or {}).get("confirmed"))) if st.get("submit") else "")
                # _do_fill never raises — a failed fill is state 'error'. Only a fill that ran counts
                # against the daily budget / advances the rotation; a failure is retried next run.
                if apply_campaigns.fill_counts_as_done(st):
                    done.append(int(jid))
                    total += 1
            except Exception as exc:
                log.info("campaign %s job %s ERROR %s", c.get("id"), jid, str(exc)[:160])
        if done:
            apply_campaigns.note_run(c.get("id"), done, today)
    log.info("apply-campaigns done: %d applications across %d campaigns", total, len(active))


if __name__ == "__main__":
    main()
