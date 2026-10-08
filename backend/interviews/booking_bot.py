"""Recruiter self-schedule AUTO-BOOKING for Alan Bai's собесы — reuses the project's existing
headful/stealth browser stack (`applier.browser.BrowserManager` + `_STEALTH` + `filler.human_type`)
to drive the JS-SPA booking pages (Calendly first; ModernLoop/GoodTime best-effort) instead of
leaving them as «нужна ручная бронь».

Contract (called from `auto_reserve`):
    book_slot(booking_url, candidate={"name","email"}, window=(18,30,21,30), prefer="20:00",
              dry_run=True) -> {"booked":bool, "when":datetime|None, "provider":str, "reason":str}

SAFETY — mirrors the apply lanes' `*_ADVANCE` gates:
  * `dry_run=True` is the DEFAULT and loads the page, finds the in-window slot, (optionally) fills the
    form but NEVER clicks the final "Schedule" — side-effect-free, screenshots to logs/booking/.
  * a LIVE booking happens only when `dry_run=False` AND `BOOKING_ADVANCE=1`.
  * NEVER reports booked=True without the provider's on-page confirmation; any captcha/anti-bot wall
    or unsupported provider → booked=False + reason (caller keeps the manual flag).
  * concurrency 1, bounded timeouts, never spins — :98 is shared with the apply/assessment lanes.

The slot PICKER (`pick_slot`) is pure + unit-tested; the browser drive is guarded (never raises).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "booking")
_NAV_TIMEOUT = 45_000
_STEP_TIMEOUT = 20_000


# --------------------------------------------------------------------------------------------------
# PURE slot selection
# --------------------------------------------------------------------------------------------------
def _hhmm(prefer: str) -> int:
    try:
        h, m = prefer.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 20 * 60


def pick_slot(offered, window=(18, 30, 21, 30), prefer="20:00"):
    """From a list of offered tz-aware datetimes pick the one to book: EARLIEST day, and within that
    day the time inside [window] CLOSEST to `prefer` (default 20:00). Returns a datetime or None.
    Window is (start_h, start_m, end_h, end_m) in the slot's own local time."""
    sh, sm, eh, em = window
    lo, hi, pref = sh * 60 + sm, eh * 60 + em, _hhmm(prefer)
    in_win = []
    for s in offered:
        if s is None:
            continue
        mins = s.hour * 60 + s.minute
        if lo <= mins <= hi:
            in_win.append(s)
    if not in_win:
        return None
    earliest_day = min(s.date() for s in in_win)
    same_day = [s for s in in_win if s.date() == earliest_day]
    return min(same_day, key=lambda s: abs((s.hour * 60 + s.minute) - pref))


def detect_provider(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if "calendly.com" in host:
        return "calendly"
    if "modernloop" in host:
        return "modernloop"
    if "goodtime" in host or "app.goodtime.io" in host:
        return "goodtime"
    return "unknown"


def _result(booked=False, when=None, provider="unknown", reason=""):
    return {"booked": booked, "when": when, "provider": provider, "reason": reason}


# --------------------------------------------------------------------------------------------------
# Calendly driver (the one provider with a stable public SPA)
# --------------------------------------------------------------------------------------------------
async def _read_calendly_slots(page):
    """Return offered tz-aware datetimes across the first few available days. Calendly exposes each
    time as a <button data-start-time="ISO8601"> once a day is selected; days are buttons whose
    aria-label contains 'Times available'. Bounded to ~6 day-clicks."""
    import json as _json
    offered: list = []
    # fast path — the embedded availability JSON sometimes carries data-start-time on buttons directly
    async def _collect_visible():
        btns = await page.query_selector_all("button[data-start-time]")
        for b in btns:
            iso = await b.get_attribute("data-start-time")
            if iso:
                try:
                    offered.append(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")))
                except Exception:
                    pass
    await _collect_visible()
    if offered:
        return offered
    # otherwise walk available days
    try:
        await page.wait_for_selector("button[aria-label*='Times available'], td[role='gridcell'] button[aria-label*='available']",
                                     timeout=_STEP_TIMEOUT)
    except Exception:
        return offered
    day_btns = await page.query_selector_all("button[aria-label*='Times available'], td[role='gridcell'] button[aria-label*='available']")
    for day in day_btns[:6]:
        try:
            await day.click(timeout=_STEP_TIMEOUT)
            await page.wait_for_timeout(1200)
            await _collect_visible()
            if offered:
                break
        except Exception:
            continue
    return offered


async def _book_calendly(page, cand, when, dry_run):
    """With a slot chosen, click it → fill name/email → (live only) Schedule. Returns (ok, reason)."""
    from backend.applier import filler
    iso = when.isoformat()
    btn = await page.query_selector(f"button[data-start-time='{iso}']") \
        or await page.query_selector("button[data-start-time]")
    if not btn:
        return False, "slot_button_gone"
    await btn.click(timeout=_STEP_TIMEOUT)
    # a "Next"/"Confirm" button reveals the form
    for sel in ("button[aria-label*='Next']", "button:has-text('Next')", "button:has-text('Confirm')"):
        el = await page.query_selector(sel)
        if el:
            try:
                await el.click(timeout=_STEP_TIMEOUT)
                break
            except Exception:
                pass
    try:
        await page.wait_for_selector("input[name='full_name'], input[id*='full_name'], input[name='name']",
                                     timeout=_STEP_TIMEOUT)
    except Exception:
        return False, "booking_form_not_reached"
    name_loc = page.locator("input[name='full_name'], input[id*='full_name'], input[name='name']").first
    email_loc = page.locator("input[name='email'], input[type='email']").first
    try:
        await filler.human_type(page, name_loc, cand.get("name") or "")
        await filler.human_type(page, email_loc, cand.get("email") or "")
    except Exception as exc:
        return False, f"fill_failed:{type(exc).__name__}"
    await _screenshot(page, "calendly_filled")
    if dry_run or os.environ.get("BOOKING_ADVANCE") != "1":
        return False, "dry_run"
    # LIVE: submit + confirm
    for sel in ("button:has-text('Schedule Event')", "button[type='submit']:has-text('Schedule')",
                "button:has-text('Schedule')"):
        el = await page.query_selector(sel)
        if el:
            await el.click(timeout=_STEP_TIMEOUT)
            break
    else:
        return False, "schedule_button_not_found"
    try:
        await page.wait_for_selector("text=/You are scheduled|scheduled with|confirmed/i", timeout=_NAV_TIMEOUT)
    except Exception:
        return False, "no_confirmation"
    await _screenshot(page, "calendly_confirmed")
    return True, "confirmed"


async def _screenshot(page, tag: str):
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        await page.screenshot(path=os.path.join(_LOG_DIR, f"{ts}_{tag}.png"))
    except Exception:
        pass


async def _drive(booking_url, candidate, window, prefer, dry_run):
    from backend.applier.browser import BrowserManager
    provider = detect_provider(booking_url)
    if provider != "calendly":
        return _result(provider=provider, reason="provider_unsupported")
    headful = os.environ.get("BOOKING_HEADFUL") == "1"
    try:
        async with BrowserManager(headless=not headful) as bm:
            ctx = await bm.new_context()
            page = await ctx.new_page()
            try:
                await page.goto(booking_url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                await _screenshot(page, "calendly_landed")
                offered = await _read_calendly_slots(page)
                when = pick_slot(offered, window=window, prefer=prefer)
                if when is None:
                    return _result(provider=provider, reason=f"no_in_window_slot(offered={len(offered)})")
                ok, reason = await _book_calendly(page, candidate, when, dry_run)
                return _result(booked=ok, when=when if ok else None, provider=provider,
                               reason=(reason if ok else f"would_book:{when.isoformat()}" if reason == "dry_run" else reason))
            finally:
                await ctx.close()
    except Exception as exc:                     # never break the caller
        log.warning("booking_bot drive failed: %s", exc)
        return _result(provider=provider, reason=f"error:{type(exc).__name__}")


def book_slot(booking_url, *, candidate, window=(18, 30, 21, 30), prefer="20:00", dry_run=True):
    """Sync entry point for `auto_reserve`. Runs the async driver; never raises."""
    if not booking_url:
        return _result(reason="no_url")
    try:
        return asyncio.run(_drive(booking_url, candidate or {}, window, prefer, dry_run))
    except RuntimeError:                         # already inside a loop — use a fresh one
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_drive(booking_url, candidate or {}, window, prefer, dry_run))
        finally:
            loop.close()
    except Exception as exc:
        return _result(reason=f"error:{type(exc).__name__}")
