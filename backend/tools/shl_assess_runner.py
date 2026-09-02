"""Autonomous SHL assessment runner.

Finds the Maximus SHL assessment invites sitting in our persona mailboxes and drives each one to
completion with the etalon engine (`shl_assessment.run_intro(..., complete_scored=True)`) — repeats
answered instantly from the answer bank, novel judgement items decided by the local model and then
banked, so it gets faster and cheaper the more it runs. Completed invites are remembered so they are
never re-run. A file lock makes it safe to schedule (cron) without overlapping runs.

    python -m backend.tools.shl_assess_runner            # complete all pending invites once
    python -m backend.tools.shl_assess_runner --list     # just show pending / completed
    python -m backend.tools.shl_assess_runner --concurrency 2
    python -m backend.tools.shl_assess_runner --upgrade-bank   # offline: re-decide judgement bank
                                                               # entries with the model (no browser)

Headful is mandatory (SHL rejects headless) — run under DISPLAY=:98 and `sg mail` (reads Maildirs).
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
logger = logging.getLogger("shl_assess_runner")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.tools import mail_db, shl_assessment as sa  # noqa: E402

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
STATE_PATH = os.path.join(_DATA, "shl_assess_state.json")
DONE_PATH = os.path.join(_DATA, "shl_assess_done.json")  # mailboxes whose OPQ we've completed
LOCK_PATH = os.path.join(_DATA, "shl_assess_runner.lock")
PERSONA = {"country": "United States", "education_level": "Bachelor"}
_LINK_RE = re.compile(r"https?://integration-talentcentral[^\s\"<>\\)]+")


# ---- state (which invites are already completed) -------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)
    os.replace(tmp, STATE_PATH)


def _mark(link: str, status: str) -> None:
    state = _load_state()
    state[link] = status
    _save_state(state)


def _mark_assessment_done(name: str) -> None:
    """Record that this persona's OPQ is done so the CRM stops flagging its invite as
    `action_needed`: (1) persist the mailbox to shl_assess_done.json (so a re-index keeps the
    `assessment_done` tag — read by mailcrm.build_index_row), and (2) re-tag the already-indexed
    invite row NOW for an immediate CRM effect. Best-effort; never breaks the run."""
    email = name if "@" in name else f"{name}@takhet.com"
    try:
        done = set(json.load(open(DONE_PATH))) if os.path.exists(DONE_PATH) else set()
    except Exception:
        done = set()
    if email not in done:
        done.add(email)
        try:
            tmp = f"{DONE_PATH}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(sorted(done), f)
            os.replace(tmp, DONE_PATH)
        except Exception:
            pass
    try:
        with mail_db.conn() as c:
            cur = c.cursor()
            cur.execute("UPDATE mail_index SET kind='assessment_done' "
                        "WHERE mailbox=%s AND kind='action_needed' AND subject ILIKE %s",
                        (email, "%complete your assessment%"))
    except Exception:
        pass


# ---- discovery (pending invites from the mailboxes) ---------------------------------------------
def _link_from_path(path: str):
    try:
        txt = open(path, "rb").read().decode("utf-8", "ignore").replace("=\r\n", "").replace("=\n", "")
    except Exception:
        return None
    m = sorted(set(_LINK_RE.findall(txt)))
    return m[0].replace("3D", "") if m else None  # 'rid=3D..' is quoted-printable for 'rid=..'


def discover_invites() -> list[tuple[str, str]]:
    """Return [(mailbox, link)] for the newest Maximus assessment invite per persona mailbox."""
    sql = """SELECT DISTINCT ON (mailbox) mailbox, path FROM mail_index
             WHERE from_email ILIKE '%maximus%' AND subject ILIKE '%complete your assessment%'
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


_TERMINAL = ("completed", "needs_human")


def pending_invites() -> list[tuple[str, str]]:
    """Invites not yet in a terminal state. 'incomplete:*' is terminal too (run_one already retried
    it MAX_RETRIES times) so a drain loop can't spin on it forever; clear its state to retry."""
    state = _load_state()

    def done(link: str) -> bool:
        s = state.get(link, "")
        return s in _TERMINAL or s.startswith("incomplete")

    return [(n, l) for n, l in discover_invites() if not done(l)]


# ---- run one assessment to completion ------------------------------------------------------------
async def _already_done(page) -> bool:
    """RELIABLE done-signal only: the assessment overview genuinely showing '0 assessment(s) left',
    OR an explicit SHL completion confirmation (_COMPLETE_RE). The old loose `"completed" in body and
    "assessment" in body` matched the overview chrome / a partially-done page and marked NOT-done
    assessments completed (owner-verified false-completion — a persona at 0%/Start was flagged done)."""
    try:
        body = (await page.inner_text("body", timeout=3000)).lower()
    except Exception:
        body = ""
    # NB the real SHL overview renders "0Assessment(s) left" with NO space, so match \b0\s*assessment
    # (a plain "0 assessment" substring never matched — which is why the old code leaned on the weak
    # "/opq/ not in url" heuristic that caused the false completions).
    return (bool(re.search(r"\b0\s*assessment", body)) and "left" in body) or bool(sa._COMPLETE_RE.search(body))


async def run_one(name: str, link: str, *, max_retries: int = 6) -> str:
    from playwright.async_api import async_playwright
    logger.info("[%s] start", name)
    last = "?"
    for attempt in range(1, max_retries + 1):
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=False, args=["--no-sandbox"], timeout=60000)
            pg = await b.new_page(viewport={"width": 1280, "height": 850})
            try:
                # WATCHDOG: a single assessment must never hang forever (a stuck run held the drain
                # lock ~56 min once — display contention). A real OPQ finishes in ~5-8 min, so cap at
                # 15 min per attempt; a timeout is treated as a failed attempt and retried on a fresh
                # browser (or, after max_retries, left incomplete for the next run).
                res = await asyncio.wait_for(
                    sa.run_intro(link, PERSONA, page=pg, max_steps=18, complete_scored=True),
                    timeout=900)
            except asyncio.TimeoutError:
                res = {"status": "error", "note": "timeout 900s — assessment hung, retrying"}
            except Exception as e:
                res = {"status": "error", "note": f"{type(e).__name__}: {e}"[:120]}
            last = res.get("status")
            logger.info("[%s] attempt %d: %s progress=%s%% answered=%s note=%s", name, attempt,
                        last, res.get("last_progress"), res.get("items_answered"),
                        (res.get("note", "") or "")[:70])
            # Only a real completion signal marks done; never let a stale/overview page override a
            # needs_human (ability item) or an error into a false "completed".
            done = last == "completed" or (last != "needs_human" and await _already_done(pg))
            await b.close()
        if done:
            _mark(link, "completed")
            _mark_assessment_done(name)  # stop the CRM flagging this invite as action_needed
            logger.info("[%s] COMPLETED", name)
            return "completed"
        if last == "needs_human" or "ability" in (res.get("note", "") or ""):
            _mark(link, "needs_human")
            logger.info("[%s] NEEDS_HUMAN (%s)", name, res.get("note", "")[:60])
            return "needs_human"
        await asyncio.sleep(3)  # resume/retry a transient stall on a fresh browser
    _mark(link, f"incomplete:{last}")
    logger.info("[%s] gave up after %d attempts (last=%s)", name, max_retries, last)
    return f"incomplete:{last}"


async def run_all(concurrency: int = 3) -> dict:
    invites = pending_invites()
    if not invites:
        logger.info("no pending assessment invites")
        return {}
    logger.info("pending invites: %d", len(invites))
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(n, l):
        async with sem:
            return n, await run_one(n, l)

    results = dict(await asyncio.gather(*(_guarded(n, l) for n, l in invites)))
    logger.info("bank now holds %d distinct items", sa.bank_size())
    return results


# ---- offline: upgrade banked judgement answers to model quality ---------------------------------
async def upgrade_bank() -> dict:
    """Re-decide every banked SJT / forced-choice item with the local model (no browser). Repeats
    stay consistent; the deterministic picks a fast run recorded become model-quality."""
    bank = sa._bank_load()
    judged = [(k, v) for k, v in bank.items()
              if v.get("kind") in ("sjt", "forced_choice") and v.get("options")]
    logger.info("upgrading %d judgement bank entries", len(judged))
    changed = 0
    for i, (key, entry) in enumerate(judged, 1):
        opts = entry["options"]
        idx = await sa._llm_pick(entry.get("q", ""), opts)
        if idx is not None:
            new_ans = sa._bank_norm(opts[idx])
            if new_ans != entry.get("answer"):
                entry["answer"] = new_ans
                changed += 1
        if i % 20 == 0:
            sa._bank_save()
            logger.info("  upgraded %d/%d (changed so far %d)", i, len(judged), changed)
    sa._bank_save()
    logger.info("upgrade done: %d entries, %d answers changed", len(judged), changed)
    return {"entries": len(judged), "changed": changed}


def watch(concurrency: int = 2, interval: int = 60) -> None:
    """Daemon: complete each assessment the MOMENT its invite lands (near-instant — polls the mail
    index every `interval`s, so no 3h cron wait). Runs forever; a per-tick try/except keeps it up."""
    import time
    logger.info("shl watch started (interval=%ss, concurrency=%d)", interval, concurrency)
    while True:
        try:
            if pending_invites():
                asyncio.run(run_all(concurrency))
        except Exception:
            import traceback
            logger.warning("watch tick error: %s", traceback.format_exc().splitlines()[-1])
        time.sleep(interval)


def _acquire_lock():
    os.makedirs(_DATA, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("another runner holds the lock — exiting")
        sys.exit(0)
    return f  # keep the handle alive for the process lifetime


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show pending/completed invites and exit")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--upgrade-bank", action="store_true", help="offline: re-decide judgement bank")
    ap.add_argument("--watch", action="store_true", help="daemon: complete invites the moment they arrive")
    ap.add_argument("--interval", type=int, default=60, help="--watch poll seconds")
    ap.add_argument("--drain", action="store_true",
                    help="complete every pending invite, looping until none remain, then exit "
                         "(what the mail-indexer hook spawns on a new invite)")
    args = ap.parse_args()

    if args.list:
        state = _load_state()
        inv = discover_invites()
        print(f"invites: {len(inv)}   bank distinct items: {sa.bank_size()}")
        for n, l in inv:
            print(f"  {n:26} {state.get(l, 'pending')}")
        return

    _lock = _acquire_lock()  # noqa: F841  (held for process lifetime)
    if args.upgrade_bank:
        asyncio.run(upgrade_bank())
        return
    if args.watch:
        watch(args.concurrency, args.interval)
        return
    if args.drain:
        # loop so invites that LAND DURING the drain are still picked up before we exit. A 40-min
        # cap is a backstop so a drain can NEVER hold the lock indefinitely — if work remains, the
        # next invite (mail-indexer hook) or cron spawns a fresh drain that reclaims the lock.
        import time as _t
        start = _t.time()
        while _t.time() - start < 2400:
            if not asyncio.run(run_all(args.concurrency)):
                break
        return
    results = asyncio.run(run_all(args.concurrency))
    print("\n==== SUMMARY ====")
    for name, status in results.items():
        print(f"  {name:26} {status}")


if __name__ == "__main__":
    main()
