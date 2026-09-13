"""Hallo.ai adapter — TP's NEW post-apply assessment (`app.hallo.ai/tp-global-us/ai-assessment/<token>`),
which SUPERSEDED the AMCAT/Aspiring-Minds battery (TP now emails Hallo invites from support@hallo.ai,
not talentcentral@shl.com). The assessment battery is STRUCTURALLY the same as AMCAT — the overview
lists: 1. TA Questionnaire (spoken) · 2. Language Assessments · 3. Typing · 4. Personality · 5.
Cognitive · 6. Hardskill — so the generic Adapter answering (SVAR speaking via the pulse virtmic,
typing, MCQ personality/cognitive) is reused; only the ENTRY (a React device-check gate) is Hallo-
specific and lives here.

DEVICE-CHECK (proven live 2026-09-13, the hard part): name gate (First/Last = the persona's registered
name) → Assessment Honor Code (tick all) → "Check microphone and camera": the mic auto-listens (a
GAPLESS speech feed into the virtmic makes its meter go green — Chromium captures the virtmic at RMS
~0.2, verified) + a REAL v4l2loopback camera makes the camera check pass (unlike Sutherland's WCI200,
Hallo ACCEPTS the virtual camera — a dark feed) + an internet speed test (auto) + a bottom "I understand
that ..." consent checkbox (must be ticked) → Continue enables → "Start Questionnaire" enters the
battery. Requires the persistent camera_daemon (a live /dev/video0) + mic.ensure()/launch_env().
"""
from __future__ import annotations

import os
import re
import subprocess

from .base import Adapter


class HalloAdapter(Adapter):
    platform = "hallo"

    def __init__(self, mailbox: str = ""):
        # First/Last name for the entry gate = the persona's registered applicant name, derived from
        # the mailbox local part (first.last<N>@takhet.com). Set by harvest_runner before enter().
        self.mailbox = mailbox
        self._mic_feed: subprocess.Popen | None = None

    def _name(self) -> tuple[str, str]:
        local = (self.mailbox or "candidate.user").split("@")[0]
        parts = local.split(".")
        first = (parts[0] or "Candidate").capitalize()
        last = re.sub(r"\d+$", "", parts[1]).capitalize() if len(parts) > 1 and parts[1] else "User"
        return first, last or "User"

    def _start_mic_feed(self) -> None:
        """GAPLESS continuous speech into the virtmic sink so Hallo's auto-listening mic meter always
        samples live voice (a looped paplay leaves silent gaps the meter fails on)."""
        try:
            from backend.tools.assessment_harvester import assets, mic
            wav = (assets.ensure_assets() or {}).get("audio")
            if not wav or not os.path.exists(wav):
                return
            self._mic_feed = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-re",
                 "-i", wav, "-f", "pulse", "-device", mic.SINK, "hallo_devcheck"],
                env=mic._env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self._mic_feed = None

    def _stop_mic_feed(self) -> None:
        try:
            if self._mic_feed:
                self._mic_feed.kill()
        except Exception:
            pass
        self._mic_feed = None

    async def _click(self, page, rx: str, timeout: int = 3000) -> bool:
        try:
            b = page.get_by_role("button", name=re.compile("^" + rx, re.I)).first
            if await b.count() and await b.is_enabled():
                await b.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    async def _tick_all(self, page) -> None:
        try:
            cbs = page.locator('input[type=checkbox]')
            for i in range(await cbs.count()):
                c = cbs.nth(i)
                try:
                    if not await c.is_checked():
                        await c.check(timeout=1500, force=True)
                except Exception:
                    try:
                        await c.click(timeout=1500, force=True)
                    except Exception:
                        pass
        except Exception:
            pass

    async def _continue_enabled(self, page) -> bool:
        try:
            return await page.evaluate(
                "() => { const c=[...document.querySelectorAll('button')]"
                ".find(b=>/^continue/i.test((b.innerText||'').trim())); return c? !c.disabled : false; }")
        except Exception:
            return False

    async def enter(self, page, url: str) -> None:
        first, last = self._name()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)
        await self._click(page, "Accept")            # cookie consent
        await page.wait_for_timeout(1000)
        # (1) NAME GATE
        try:
            tis = page.locator('input[type=text], input:not([type])')
            if await tis.count() >= 2:
                await tis.nth(0).fill(first, timeout=4000)
                await tis.nth(1).fill(last, timeout=4000)
        except Exception:
            pass
        await self._tick_all(page)
        await self._click(page, "Continue")
        await page.wait_for_timeout(3500)
        # (2) HONOR CODE — tick every clause + continue
        await self._tick_all(page)
        await self._click(page, "Continue")
        await page.wait_for_timeout(6000)
        # (3) DEVICE CHECK — real camera (daemon) + gapless virtmic feed + consent + internet, then Continue
        self._start_mic_feed()
        try:
            await self._click(page, "Start")          # Start Camera
            await page.wait_for_timeout(3000)
            await self._click(page, "Retry")          # (re)run the mic listen with audio flowing
            await page.wait_for_timeout(6000)
            try:
                await page.mouse.wheel(0, 600)        # bring the bottom 'I understand' consent into view
                await page.wait_for_timeout(500)
            except Exception:
                pass
            await self._tick_all(page)
            for _ in range(24):                       # wait for the internet test + Continue to enable
                if await self._continue_enabled(page):
                    await self._click(page, "Continue")
                    break
                await self._tick_all(page)
                await page.wait_for_timeout(5000)
        finally:
            self._stop_mic_feed()
        await page.wait_for_timeout(6000)
        # (4) enter the battery
        await self._click(page, "Start Questionnaire") or await self._click(page, "Start")
        await page.wait_for_timeout(4000)

    async def wall(self, page) -> str | None:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            return None
        if "won't be able to proceed" in body and ("camera" in body or "microphone" in body):
            # only a WALL if we're stuck ON the device-check (Continue never enabled)
            if not await self._continue_enabled(page):
                return "device_check"
        if re.search(r"you have been (logged out|removed)|integrity (violation|check failed)", body):
            return "proctor"
        return None
