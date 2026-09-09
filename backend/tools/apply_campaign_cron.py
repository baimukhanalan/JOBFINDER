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
        done, attempted = [], []
        for jid in targets:
            try:
                # a fresh mailbox + persona id per application (same name), unless the campaign
                # is pinned to one mailbox; wait_submit so the emailed code is finished inline
                email, pid = apply_campaigns.next_identity(c.get("id"))
                log.info("campaign %s job %s as %s <%s>", c.get("id"), jid, c.get("name"), email)
                _do_fill(int(jid), c.get("gender") or None, c.get("name"), email, pid,
                         wait_submit=True)
                attempted.append(int(jid))
                st = _FILL_JOBS.get(int(jid), {}) or {}
                sub = st.get("submit") or {}
                log.info("campaign %s job %s -> %s%s", c.get("id"), jid, st.get("state"),
                         (" submit=" + str(sub.get("reason") or sub.get("confirmed"))) if sub else "")
                # A posting the co-pilot found NO form for is gone at the ATS (the first live run
                # hit one) — mark it dead so the rotation skips it from now on.
                if apply_campaigns.fill_is_dead_posting(st):
                    try:
                        from backend.tools import catalog_db
                        row = catalog_db.jobs_by_ids([int(jid)]).get(int(jid)) or {}
                        if row.get("ats") and row.get("external_id"):
                            catalog_db.mark_dead([(row["ats"], row.get("company_key"), row["external_id"])],
                                                 "campaign: no form at the ATS (posting gone)")
                            log.info("campaign %s job %s marked DEAD (no form)", c.get("id"), jid)
                    except Exception as exc:
                        log.info("campaign %s job %s mark_dead failed: %s", c.get("id"), jid, str(exc)[:120])
                # Only a fill that really pressed Submit spends the daily budget; anything else is
                # retried on a later lap (the cursor still moves past it).
                if apply_campaigns.fill_counts_as_done(st):
                    done.append(int(jid))
                    total += 1
            except Exception as exc:
                log.info("campaign %s job %s ERROR %s", c.get("id"), jid, str(exc)[:160])
        if attempted:
            apply_campaigns.note_run(c.get("id"), done, today, attempted=attempted)
    log.info("apply-campaigns done: %d applications across %d campaigns", total, len(active))


if __name__ == "__main__":
    main()
