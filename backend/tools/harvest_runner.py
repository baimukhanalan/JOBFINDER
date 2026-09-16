"""Autonomous assessment question-bank HARVESTER runner.

Discovers assessment invites in the synthetic-persona Maildirs (per platform), drives each with a
fresh headful browser answering RANDOMLY, and banks every question + options + screenshots into
`backend/data/assessment_bank.json`. Fake mic/camera (see core) drive past speaking/listening/video.

    python -m backend.tools.harvest_runner --platform shl_sutherland --limit 1
    python -m backend.tools.harvest_runner --platform amcat --list
    python -m backend.tools.harvest_runner --platform shl_sutherland --limit 5 --concurrency 2

Headful only (SHL/AMCAT reject headless) — run under DISPLAY=:98 + `sg mail` (reads Maildirs).
SYNTHETIC personas only; random answers deliberately FAIL the scored test — the point is to CAPTURE.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("harvest_runner")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools.assessment_harvester import bank, core, discover  # noqa: E402
from backend.tools.assessment_harvester.adapters.amcat import AmcatAdapter  # noqa: E402
from backend.tools.assessment_harvester.adapters.hallo import HalloAdapter  # noqa: E402
from backend.tools.assessment_harvester.adapters.shl import ShlAdapter  # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
LOCK_PATH = os.path.join(_DATA, "harvest_runner.lock")

ADAPTERS = {"amcat": AmcatAdapter, "shl_sutherland": ShlAdapter, "hallo": HalloAdapter}


def _adapter(platform: str, mailbox: str = ""):
    cls = ADAPTERS.get(platform)
    if not cls:
        raise SystemExit(f"no adapter for platform {platform!r} (have {list(ADAPTERS)})")
    # HalloAdapter needs the persona name (from the mailbox) for its device-check name gate.
    try:
        return cls(mailbox=mailbox) if platform == "hallo" else cls()
    except TypeError:
        a = cls()
        try:
            a.mailbox = mailbox
        except Exception:
            pass
        return a


async def run(platform: str, limit: int, concurrency: int) -> dict:
    invites = discover.discover(platform, limit=limit)
    if not invites:
        logger.info("no pending %s invites", platform)
        return {}
    logger.info("%s: %d invite(s) to harvest", platform, len(invites))
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(mbx, url):
        async with sem:
            res = await core.harvest_one(url, mbx, _adapter(platform, mbx))
            status = res.get("status", "error")
            discover.mark(url, f"{status}:banked{res.get('banked', 0)}")
            # A REAL end-to-end completion (adapter.is_done fired) is a PASS — mark the CRM invite
            # done so it leaves «Действие», exactly like the SHL-OPQ lane. Only "completed" triggers
            # this (a stuck/partial harvest never does), so it can't false-mark an unfinished test.
            if status == "completed":
                try:
                    from backend.tools import mailcrm
                    mailcrm.mark_assessment_done(mbx)
                    logger.info("[%s] COMPLETED — marked assessment done", mbx)
                except Exception as exc:
                    logger.info("[%s] mark_assessment_done failed: %s", mbx, str(exc)[:100])
            logger.info("[%s] %s banked=%d by_type=%s note=%s", mbx, status,
                        res.get("banked", 0), res.get("by_type"), (res.get("note") or "")[:80])
            return mbx, res

    results = dict(await asyncio.gather(*(_one(m, u) for m, u in invites)))
    logger.info("bank now holds %d distinct items", bank.size())
    return results


def _acquire_lock():
    os.makedirs(_DATA, exist_ok=True)
    lock_path = LOCK_PATH
    if os.getenv("HARVEST_CDP_URL"):
        # CDP mode drives a REMOTE browser per tab; the global single-instance guard (which protects
        # the server's one local browser / virtmic / camera) does not apply. Use a PER-TAB lock so two
        # tabs run concurrently, while the same tab still can't be double-driven.
        lock_path = f"{LOCK_PATH}.cdp{os.getenv('HARVEST_CDP_TAB_INDEX', 'x')}"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("another harvester holds the lock — exiting")
        sys.exit(0)
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="shl_sutherland", choices=list(ADAPTERS) + list(discover.MATCHERS))
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--list", action="store_true", help="show pending invites + bank stats and exit")
    ap.add_argument("--url", default=None, help="harvest ONE explicit invite URL (bypass discovery — for a just-arrived fresh token)")
    ap.add_argument("--mailbox", default="", help="mailbox label for --url mode")
    args = ap.parse_args()

    if args.url:
        adapter = _adapter(args.platform, args.mailbox or "")
        _lock = _acquire_lock()  # noqa: F841
        res = asyncio.run(core.harvest_one(args.url, args.mailbox or "manual", adapter,
                                           min_delay=0.0, max_delay=0.0))
        # A REAL completion marks the CRM invite «пройдено» (leaves «Действие»), same as the discover
        # path — so the dashboard reflects what the Mac lane passed. Guarded on status=="completed"
        # (never false-marks a stuck/partial run) + a real persona email (contains '@'), since --mailbox
        # may be a bare label.
        if res.get("status") == "completed" and "@" in (args.mailbox or ""):
            try:
                from backend.tools import mailcrm
                mailcrm.mark_assessment_done(args.mailbox)
                print("marked assessment done (пройдено):", args.mailbox)
            except Exception as exc:
                print("mark_assessment_done failed:", str(exc)[:100])
        print("\n==== SINGLE-URL HARVEST ====")
        print("status :", res.get("status"))
        print("banked :", res.get("banked"), "by_type:", res.get("by_type"))
        print("note   :", res.get("note"))
        print("shots  :", res.get("shots"))
        print("walls  :", res.get("walls"))
        print(f"bank total: {bank.size()} items")
        return

    if args.list:
        inv = discover.discover(args.platform, limit=args.limit or 50, include_done=True)
        st = discover.load_state()
        print(f"{args.platform}: {len(inv)} invite(s); bank distinct items: {bank.size()}")
        print("bank by platform:", bank.counts_by("platform"))
        print("bank by item_type:", bank.counts_by("item_type"))
        for mbx, url in inv[:20]:
            print(f"  {mbx:26} {st.get(url, 'pending')}")
        return

    if args.platform not in ADAPTERS:
        raise SystemExit(f"platform {args.platform!r} has a discovery matcher but no driver yet "
                         f"(drivers: {list(ADAPTERS)})")
    _lock = _acquire_lock()  # noqa: F841
    results = asyncio.run(run(args.platform, args.limit, args.concurrency))
    print("\n==== HARVEST SUMMARY ====")
    for mbx, res in results.items():
        print(f"  {mbx:26} {res.get('status'):20} banked={res.get('banked')} {res.get('by_type')}")
    print(f"bank total: {bank.size()} items  by_type={bank.counts_by('item_type')}")


if __name__ == "__main__":
    main()
