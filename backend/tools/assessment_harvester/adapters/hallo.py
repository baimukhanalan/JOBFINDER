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

import logging
import os
import re
import subprocess

from .base import Adapter

logger = logging.getLogger("assessment_harvester")


class HalloAdapter(Adapter):
    platform = "hallo"

    def __init__(self, mailbox: str = ""):
        # First/Last name for the entry gate = the persona's registered applicant name, derived from
        # the mailbox local part (first.last<N>@takhet.com). Set by harvest_runner before enter().
        self.mailbox = mailbox
        self._mic_feed: subprocess.Popen | None = None
        self._prep_waits = 0    # consecutive prep/recording auto-waits (bounded so a stuck one can't spin)
        self._last_prog = ""    # last seen "Part N / Question k of M" marker — resets the wait budget on progress

    def _name(self) -> tuple[str, str]:
        local = (self.mailbox or "candidate.user").split("@")[0]
        parts = local.split(".")
        first = (parts[0] or "Candidate").capitalize()
        last = re.sub(r"\d+$", "", parts[1]).capitalize() if len(parts) > 1 and parts[1] else "User"
        return first, last or "User"

    @staticmethod
    def _progress_sig(body: str) -> str:
        """A stable per-question marker ("part 1 · question 3 of 5") pulled from a listening page so
        advance() can tell a page that ADVANCED (reset the wait budget) from one that is truly frozen.
        Keyed on the question INDEX, not the raw body, so a per-second countdown does NOT keep resetting
        the budget (that would defeat the frozen-page bound on a recording/prep page)."""
        m_part = re.search(r"part\s+(\d+)", body)
        m_q = re.search(r"question\s+(\d+)\s+of\s+(\d+)", body)
        if not m_part and not m_q:
            return ""
        return f"{m_part.group(1) if m_part else '?'}:{m_q.group(0) if m_q else '?'}"

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

    async def _log_devcheck_controls(self, page) -> None:
        """Dump every clickable control (label / aria-label / title / disabled) + any <audio> on the
        device-check page so a wall reveals the EXACT record/stop/playback button names — the
        screenshot shows a mic 'recording test' whose sample must be PLAYED BACK before Continue
        enables, but the button labels aren't guessable from the shot alone."""
        try:
            ctrls = await page.evaluate(
                "() => { const t=[]; for (const b of document.querySelectorAll("
                "'button,[role=button],a')) { const s=((b.innerText||'')+'|'+(b.getAttribute('aria-label')"
                "||'')+'|'+(b.getAttribute('title')||'')).replace(/\\s+/g,' ').trim(); "
                "if (s.replace(/\\|/g,'')) t.push(s+(b.disabled?' [disabled]':'')); } "
                "const au=document.querySelectorAll('audio,video').length; "
                "return {btns:t.slice(0,40), media:au}; }")
            logger.info("[hallo] device-check controls: %s | media_els=%s",
                        ctrls.get("btns"), ctrls.get("media"))
        except Exception as e:
            logger.info("[hallo] device-check control dump failed: %s", e)

    async def _mic_record_playback(self, page) -> None:
        """Complete Hallo's mic 'recording test': RECORD a few seconds of the looped voice feed, STOP,
        then PLAY BACK the sample — the step that enables Continue. Tolerant to the exact labels (Record/
        Start Recording, Stop/Stop Recording, Play Back/Playback/Play/Listen/▶); each click is a no-op
        when that control is absent, so running the whole cycle is safe regardless of which stage the UI
        is in."""
        for rec in ("Record", "Start Recording", "Test", "Start"):
            if await self._click(page, rec, timeout=1500):
                break
        await page.wait_for_timeout(4000)                      # capture a few seconds of live voice
        for stop in ("Stop Recording", "Stop", "Done"):
            if await self._click(page, stop, timeout=1500):
                break
        await page.wait_for_timeout(1500)
        for pb in ("Play Back", "Play back", "Playback", "Play Sample", "Play", "Listen", "▶"):
            if await self._click(page, pb, timeout=1500):
                await page.wait_for_timeout(4500)              # let the sample play to the end
                break

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
        # (3) DEVICE CHECK — real camera (daemon, ACCEPTED by Hallo) + gapless virtmic feed + consent +
        # internet, then the mic RECORD→STOP→PLAY-BACK cycle. The camera passes on the virtual device;
        # the wall (run 2026-09-14) was the mic 'recording test': Hallo says "complete the recording test
        # and play back your sample before continuing", and Continue stays disabled until the recorded
        # sample is PLAYED BACK. The old flow only Start/Retry'd (recorded, never stopped+played).
        self._start_mic_feed()
        try:
            await self._click(page, "Start")          # Start Camera
            await page.wait_for_timeout(3000)
            await self._log_devcheck_controls(page)   # reveal the exact record/stop/playback labels
            await self._mic_record_playback(page)     # record a sample + play it back -> enables Continue
            try:
                await page.mouse.wheel(0, 600)        # bring the bottom 'I understand' consent into view
                await page.wait_for_timeout(500)
            except Exception:
                pass
            await self._tick_all(page)
            for i in range(24):                       # wait for the internet test + Continue to enable
                if await self._continue_enabled(page):
                    await self._click(page, "Continue")
                    break
                if i in (2, 6, 12):                   # re-run the cycle a few times if it raced the UI
                    await self._mic_record_playback(page)
                await self._tick_all(page)
                await page.wait_for_timeout(5000)
            else:
                await self._log_devcheck_controls(page)   # still walled — log the final control state
        finally:
            self._stop_mic_feed()
        await page.wait_for_timeout(6000)
        # (4) enter the battery
        await self._click(page, "Start Questionnaire") or await self._click(page, "Start")
        await page.wait_for_timeout(4000)

    # Hallo gates each module behind an instructions page with a module-START button ("Start Part 1",
    # "Start Questionnaire", "Begin", "I'm Ready") that the generic forward matcher misses — click it.
    _FWD = ("Start Part", "Start Questionnaire", "Begin", "I'?m Ready", "Ready to", "Start Now",
            "Start", "Continue", "Next", "Proceed", "Got it")

    async def _skip_forward(self, page) -> bool:
        """Click the REAL 'Skip' control on an instruction / video-intro page (the intended way forward)
        WITHOUT clicking the 'Skip to main content' accessibility link. Matches Skip / Skip Intro /
        Skip Video, excludes any label containing 'main content' / 'to content'. Only clicks an ENABLED
        control (a video's Skip is often disabled until it has played a few seconds — a later advance()
        pass then catches it)."""
        try:
            btns = page.get_by_role("button", name=re.compile(r"^Skip\b", re.I))
            for i in range(await btns.count()):
                b = btns.nth(i)
                try:
                    lbl = (await b.inner_text() or "").strip().lower()
                except Exception:
                    lbl = ""
                if "main content" in lbl or "to content" in lbl:
                    continue
                try:
                    if await b.is_enabled():
                        await b.click(timeout=2000)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    async def advance(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=2000)).lower()
        except Exception:
            body = ""
        # A prep/countdown/listening page ("Prepare your response", "Recording will end in N", "Listen
        # carefully to the content" + a "Write your notes" pad) AUTO-transitions and the comprehension
        # MCQs appear on that SAME page after the audio — so it must be WAITED out, NOT skipped.
        is_prep = any(s in body for s in ("prepare your response", "think about your response",
                                          "recording will end", "get ready", "preparing",
                                          "listen carefully to the content", "write your notes"))
        # FORWARD FIRST (before the prep-wait). This fixes the Q5 stuck-loop (2026-09-14): the last
        # comprehension question shares the page with the "write your notes" pad, so the old wait-first
        # order made advance() WAIT on an answerable page forever instead of clicking its Submit. Order:
        # module-start / Next / Continue (per-question) → a last-of-part Submit/Finish → a real Skip
        # (instruction/video pages only, never a listening passage — that would skip the audio).
        for rx in self._FWD:
            if await self._click(page, rx, timeout=2500):
                await page.wait_for_timeout(1200); self._stuck_advances = 0; return True
        for rx in ("Submit Answers", "Submit Assessment", "Next Part", "Save & Continue",
                   "Finish", "Complete", "Submit", "Done"):
            if await self._click(page, rx, timeout=2000):
                await page.wait_for_timeout(1200); self._stuck_advances = 0; return True
        if not is_prep and await self._skip_forward(page):
            await page.wait_for_timeout(1200); self._stuck_advances = 0; return True
        # No forward control — a prep/listening page auto-transitions; WAIT (bounded, budget resets on
        # real "Part N / Question k of M" progress so a long listening module doesn't trip it).
        if is_prep:
            prog = self._progress_sig(body)
            if prog and prog != self._last_prog:
                self._last_prog = prog
                self._prep_waits = 0
                self._prep_dumped = False
            self._prep_waits += 1
            # A comprehension page keeps the "write your notes" pad (→ is_prep) even after its MCQs are
            # answerable — so the LAST question (Q5) can look like a prep page and get WAITED on forever
            # instead of submitted. When stuck 3× on the SAME progress marker, dump the page's controls
            # once to reveal the real forward/submit button (the Q5 stuck-loop, still open after the
            # Submit/Finish guesses missed it).
            if self._prep_waits == 3 and not getattr(self, "_prep_dumped", False):
                self._prep_dumped = True
                await self._log_devcheck_controls(page)
            if self._prep_waits <= 30:          # ~120s on ONE unchanging page; extends across questions
                await page.wait_for_timeout(4000)
                return True
        else:
            self._prep_waits = 0
        # Truly stuck — dump the page's clickable controls ONCE per stuck streak so a future run reveals
        # the EXACT label to add (diagnostic-first, don't keep guessing blindly).
        self._stuck_advances = getattr(self, "_stuck_advances", 0) + 1
        if self._stuck_advances == 3:
            await self._log_devcheck_controls(page)
        return await super().advance(page)

    async def try_skip(self, page) -> bool:
        # instruction/example/video-intro pages carry a Skip; use it to reach the answerable item faster.
        # Use the precise skip (excludes the 'Skip to main content' a11y link that ^Skip used to grab).
        return await self._skip_forward(page)

    async def read_item(self, page) -> dict:
        item = await super().read_item(page)
        try:
            body = (await page.inner_text("body", timeout=1500)).lower()
        except Exception:
            body = ""
        # LISTENING module: a PERSISTENT "Write your notes here while listening" scratch textarea makes
        # the generic reader classify the whole page as a typing test → the core churn-types the notes.
        # Suppress that textarea when there are no real answerable options, so the page is a transition
        # (wait for the audio + the actual questions) rather than a typing item. Real MCQ options (the
        # comprehension questions that appear after/below the audio) are kept and answered normally.
        if item.get("has_textarea") and not item.get("options") and not item.get("has_mic"):
            if "write your notes" in body or "listen carefully to the content" in body:
                item["has_textarea"] = False
        return item

    async def handle_typing(self, page, text: str) -> bool:
        """Hallo's LISTENING module ("Part N - Question k of M", a countdown timer, audio, and a
        "Write your notes here while listening to the audio" SCRATCH textarea) is NOT a typing test —
        the answerable questions appear AFTER the audio. Don't churn-type the notes (the core's
        churn-guard would stick it); wait in ONE call for the audio/notes phase to end (the questions
        then classify as MCQ), avoiding repeated typing passes. Otherwise defer to the generic typer."""
        try:
            body = (await page.inner_text("body", timeout=2000)).lower()
        except Exception:
            body = ""
        if "write your notes" in body or "listen carefully to the content" in body:
            for _ in range(30):     # up to ~90s for the audio to finish + the question to appear
                try:
                    b2 = (await page.inner_text("body", timeout=2000)).lower()
                except Exception:
                    b2 = ""
                if "write your notes" not in b2:
                    break
                await page.wait_for_timeout(3000)
            return True
        return await super().handle_typing(page, text)

    async def handle_speaking(self, page, record_secs: float = 4.0, mic_say_wav=None) -> bool:
        """Hallo open-response (Speaking) items AUTO-RECORD ~60s (a red STOP button + a 'Recording will
        end in N seconds' countdown) and score the transcribed answer. Feed a spoken answer into the
        virtmic for the window, try to STOP early, then advance to the next question. Returns True when
        the recorder is done (the item advanced)."""
        self._start_mic_feed()
        try:
            # let a few seconds of the answer record, then try to STOP early (the red circular button)
            await page.wait_for_timeout(9000)
            stopped = False
            for sel in ('[aria-label*="stop" i]', 'button:has-text("Stop")',
                        'button[class*="record" i]', 'button:has(svg)'):
                try:
                    b = page.locator(sel).first
                    if await b.count() and await b.is_visible():
                        await b.click(timeout=2000)
                        stopped = True
                        break
                except Exception:
                    pass
            # if we couldn't stop, wait for the countdown to run out (auto-advances)
            if not stopped:
                for _ in range(22):    # up to ~66s
                    try:
                        body = (await page.inner_text("body", timeout=2000)).lower()
                    except Exception:
                        body = ""
                    if "recording will end" not in body:
                        break
                    await page.wait_for_timeout(3000)
            await page.wait_for_timeout(2500)
            # advance to the next question / submit the answer if a control is shown
            for rx in ("Submit", "Next", "Continue", "Save", "Done", "Start Part"):
                if await self._click(page, rx, timeout=2000):
                    break
            await page.wait_for_timeout(2500)
            return True
        finally:
            self._stop_mic_feed()

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
