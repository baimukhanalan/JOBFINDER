"""Daily retention job: auto-delete JUNK candidate mail once volume grows,
while NEVER touching important mail or active conversations.

Policy (agreed with the user):
  * Auto-delete only "junk": messages classified kind IN ('ack','other')
    (acknowledgements, bounces/delivery-failures, spam, uncategorized) that are
    OLDER than a retention window (default 30 days).
  * NEVER delete interview / offer / rejection mail.
  * NEVER delete ANY message in a PROTECTED THREAD — a thread that contains an
    interview/offer/rejection message OR any message the candidate SENT (outbound).
  * Deleting a message = remove the Maildir file from disk AND its index row.

Run with /usr/bin/python3 from /home/projects/JOBFINDER (absolute backend.* imports):
    python3 -m backend.tools.mail_retention                 # live, 30-day window
    python3 -m backend.tools.mail_retention --dry-run       # show what would go
    python3 -m backend.tools.mail_retention --days 60       # custom window

Prints a one-line JSON summary so cron logs stay greppable.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from backend.tools import mail_db

# Only these kinds are ever eligible for deletion. interview/offer/rejection are
# excluded here AND protected at the thread level below — belt and suspenders.
DELETE_KINDS = ("ack", "other")


def _default_days() -> int:
    """Retention window: env MAIL_RETENTION_DAYS overrides the 30-day default."""
    raw = os.environ.get("MAIL_RETENTION_DAYS")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return 30


RETENTION_DAYS = _default_days()


def run(days: int, dry_run: bool) -> dict:
    """Scan deletable rows older than `days`, skip protected threads, and delete
    the rest (Maildir file + index row). Returns a summary dict."""
    cutoff = int(time.time()) - days * 86400

    protected = mail_db.protected_thread_keys()
    rows = mail_db.deletable_rows(list(DELETE_KINDS), cutoff)

    protected_skipped = 0
    hashes: list = []

    for row in rows:
        if (row["mailbox"], row["thread_key"]) in protected:
            protected_skipped += 1
            continue
        if not dry_run:
            # Best-effort file removal — a single bad path must not abort the run.
            path = row.get("path")
            if path:
                try:
                    os.remove(path)
                except (FileNotFoundError, OSError):
                    pass
        hashes.append(row["path_hash"])

    if not dry_run and hashes:
        mail_db.delete_paths(hashes)

    return {
        "scanned": len(rows),
        "deleted": len(hashes),
        "protected_skipped": protected_skipped,
        "dry_run": dry_run,
        "cutoff_days": days,
    }


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mail_retention",
        description="Auto-delete junk candidate mail older than the retention window.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=RETENTION_DAYS,
        help=f"retention window in days (default: {RETENTION_DAYS}, "
             "env MAIL_RETENTION_DAYS)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted without removing anything",
    )
    return p.parse_args(argv)


def main(argv=None) -> dict:
    args = _parse_args(argv)
    summary = run(days=args.days, dry_run=args.dry_run)
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    main()
