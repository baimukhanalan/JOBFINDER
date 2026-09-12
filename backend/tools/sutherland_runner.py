"""Sutherland (SHL TalentCentral us1) assessment runner / WCI200 wall reporter.

Finds the Sutherland SHL invites in our persona mailboxes (`talentcentral@shl.com`, subject "Your
Sutherland assessment invitation") and drives each through the SHL front-door with
`sutherland_assessment.run_sutherland`.

**Outcome on this server: `blocked_proctor_camera`.** The SHL intro passes (incl. the About-You
Submit gate + SHL's own webcam check with a dim fake feed), but the assessment then redirects to the
AMCAT/Aspiring Minds player whose WCI200 continuous proctor REJECTS the fake camera ("unable to detect
a camera") — it hard-requires a REAL camera device (NOT a face). This host has none and cannot
synthesize one (v4l2loopback's `videodev` dependency is absent from the kernel), so Sutherland
assessments are currently un-completable here. See `sutherland_assessment` for the full live finding.

    python -m backend.tools.sutherland_runner --list                 # pending / state per invite
    python -m backend.tools.sutherland_runner --verify [--persona X]  # walk to the device check only
    python -m backend.tools.sutherland_runner --persona nicholas.tucker3908   # drive one to the wall

Headful mandatory (SHL/AMCAT reject headless): run under DISPLAY=:98 + `sg mail`. Routes through a
phone egress slot (`shl_assess_runner._shl_proxy`) — override with SHL_PROXY, or SHL_DIRECT=1.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("sutherland_runner")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, shl_assessment as sa  # noqa: E402
from backend.tools import sutherland_assessment as su  # noqa: E402
from backend.tools.shl_assess_runner import _shl_proxy  # reuse the phone-egress selector  # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_LOGS = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
STATE_PATH = os.path.join(_DATA, "sutherland_assess_state.json")
LOCK_PATH = os.path.join(_DATA, "sutherland_runner.lock")
SHOTS_DIR = os.path.join(_LOGS, "sutherland_shots")
PERSONA = {"country": "United States", "education_level": "Bachelor"}
_TERMINAL = ("completed", "needs_human", "device_requires_face", "blocked_proctor_camera")


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(_DATA, exist_ok=True)
    tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)
    os.replace(tmp, STATE_PATH)


def _mark(link: str, status: str) -> None:
    state = _load_state()
    state[link] = status
    _save_state(state)


def _mark_assessment_done(name: str) -> None:
    try:
        from backend.tools import mailcrm
        mailcrm.mark_assessment_done(name)
    except Exception:
        pass


def _link_from_path(path: str):
    try:
        txt = open(path, "rb").read().decode("utf-8", "ignore").replace("=\r\n", "").replace("=\n", "")
    except Exception:
        return None
    m = su.LINK_RE.search(txt)
    return m.group(0) if m else None


def discover_invites() -> list[tuple[str, str]]:
    """[(mailbox_local, link)] for the newest Sutherland invite per persona mailbox (initial or
    reminder — the reminder carries the same assessment link)."""
    sql = """SELECT DISTINCT ON (mailbox) mailbox, path FROM mail_index
             WHERE from_email ILIKE '%talentcentral@shl.com%'
               AND subject ILIKE '%sutherland assessment invitation%'
               AND outbound = false
             ORDER BY mailbox, date_ts DESC"""
    out = []
    with mail_db.conn() as c, c.cursor() as cur:
        cur.execute(sql)
        for mailbox, path in cur.fetchall():
            link = _link_from_path(path)
            if link:
                out.append((mailbox.split("@")[0], link))
    return out


def pending_invites() -> list[tuple[str, str]]:
    state = _load_state()

    def done(link: str) -> bool:
        s = state.get(link, "")
        return s in _TERMINAL or s.startswith("incomplete")

    return [(n, l) for n, l in discover_invites() if not done(l)]


def _make_shotter(name: str):
    os.makedirs(SHOTS_DIR, exist_ok=True)

    async def _shot(page, tag):
        fn = os.path.join(SHOTS_DIR, f"{name}_{tag}.png")
        try:
            await page.screenshot(path=fn)
            logger.info("[%s] shot %s", name, fn)
        except Exception:
            pass

    return _shot


async def run_one(name: str, link: str, *, verify_only: bool = False, max_retries: int = 3) -> dict:
    from playwright.async_api import async_playwright
    logger.info("[%s] start (verify_only=%s)", name, verify_only)
    shotter = _make_shotter(name)
    last = {"status": "?", "note": ""}
    # Rotate egress slots ACROSS attempts: the WCI200 webcam check races the fake-stream init on a
    # slow slot (the KZ-mobile one), so a fresh attempt on a different slot recovers. `hash(name)` is
    # per-process-randomised, so seed the rotation from it and step by attempt.
    slots = []
    if not os.environ.get("SHL_DIRECT"):
        try:
            from backend.tools import proxy_pool
            slots = [s for s in proxy_pool.residential_slots() if s.startswith("socks5://")]
        except Exception:
            slots = []
    for attempt in range(1, max_retries + 1):
        async with async_playwright() as p:
            launch = {"headless": False, "args": su.camera_launch_args(), "timeout": 60000}
            if os.environ.get("SHL_PROXY"):
                launch["proxy"] = {"server": os.environ["SHL_PROXY"]}
                logger.info("[%s] egress via %s (env)", name, os.environ["SHL_PROXY"])
            elif slots:
                srv = slots[(abs(hash(name)) + attempt - 1) % len(slots)]
                launch["proxy"] = {"server": srv}
                logger.info("[%s] egress via %s (attempt %d)", name, srv, attempt)
            elif not os.environ.get("SHL_DIRECT"):
                px = _shl_proxy(name)
                if px:
                    launch["proxy"] = px
                    logger.info("[%s] egress via %s", name, px["server"])
            b = await p.chromium.launch(**launch)
            ctx = await b.new_context(viewport={"width": 1280, "height": 850},
                                      permissions=["microphone", "camera"])
            pg = await ctx.new_page()

            async def on_shot(tag, _pg=pg):
                await shotter(_pg, tag)

            try:
                res = await asyncio.wait_for(
                    su.run_sutherland(link, PERSONA, page=pg, complete_scored=not verify_only,
                                      verify_only=verify_only, on_shot=on_shot),
                    timeout=1200)
            except asyncio.TimeoutError:
                res = {"status": "error", "note": "timeout 1200s"}
            except Exception as e:
                res = {"status": "error", "note": f"{type(e).__name__}: {e}"[:150]}
            last = res
            logger.info("[%s] attempt %d: status=%s device=%s note=%s", name, attempt,
                        res.get("status"), res.get("device_check"), (res.get("note", "") or "")[:90])
            await b.close()

        st = res.get("status")
        if st in ("completed", "needs_human", "device_requires_face", "blocked_proctor_camera",
                  "device_passed", "reached_scored"):
            if st == "completed":
                _mark(link, "completed")
                _mark_assessment_done(name)
            elif st in ("needs_human", "device_requires_face", "blocked_proctor_camera"):
                _mark(link, st)
            return res
        await asyncio.sleep(3)  # transient stall -> fresh browser
    _mark(link, f"incomplete:{last.get('status')}")
    return last


async def run_all(concurrency: int = 1, verify_only: bool = False, only: str | None = None) -> dict:
    invites = pending_invites() if not only else [
        (n, l) for n, l in discover_invites() if n == only or n.startswith(only)]
    if not invites:
        logger.info("no matching pending Sutherland invites")
        return {}
    logger.info("pending Sutherland invites: %d%s", len(invites), f" (only={only})" if only else "")
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(n, l):
        async with sem:
            r = await run_one(n, l, verify_only=verify_only)
            return n, r.get("status")

    return dict(await asyncio.gather(*(_guarded(n, l) for n, l in invites)))


def _acquire_lock():
    os.makedirs(_DATA, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("another sutherland runner holds the lock — exiting")
        sys.exit(0)
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true", help="STOP at the device check (Step-1 verify)")
    ap.add_argument("--persona", default=None, help="drive only this persona (mailbox local part)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--drain", action="store_true", help="loop until no pending invites remain")
    args = ap.parse_args()

    if args.list:
        state = _load_state()
        inv = discover_invites()
        print(f"Sutherland invites: {len(inv)}   bank distinct items: {sa.bank_size()}")
        for n, l in inv:
            print(f"  {n:28} {state.get(l, 'pending')}")
        return

    _lock = _acquire_lock()  # noqa: F841
    if args.drain:
        import time as _t
        start = _t.time()
        while _t.time() - start < 2400:
            if not asyncio.run(run_all(args.concurrency, only=args.persona)):
                break
        return
    results = asyncio.run(run_all(args.concurrency, verify_only=args.verify, only=args.persona))
    print("\n==== SUMMARY ====")
    for name, status in results.items():
        print(f"  {name:28} {status}")


if __name__ == "__main__":
    main()
