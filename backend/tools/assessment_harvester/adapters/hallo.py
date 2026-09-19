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

    def _start_mic_feed(self, wav: str | None = None) -> None:
        """GAPLESS continuous speech into the virtmic sink so Hallo's auto-listening mic meter always
        samples live voice (a looped paplay leaves silent gaps the meter fails on). When `wav` is a
        prepared/cached spoken ANSWER, loop THAT (the real answer is scored); else loop the generic
        speech asset (device-check filler / no prepared answer)."""
        try:
            from backend.tools.assessment_harvester import assets, mic
            if not (wav and os.path.exists(wav)):
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

    async def _snap(self, page, label: str) -> None:
        """Debug screenshot at a module/flow boundary (gated on HALLO_DEBUG=1 so a production run is
        unslowed). Saves into the hallo media dir + logs the path so a live-driving loop can Read it."""
        if os.getenv("HALLO_DEBUG") != "1":
            return
        try:
            from backend.tools.assessment_harvester import media as _media
            shot = await _media.capture(page, self.platform, f"DBG:{label}", [], page.url)
            if shot:
                logger.info("[hallo] snap[%s] -> %s", label, shot)
        except Exception as e:
            logger.info("[hallo] snap[%s] failed: %s", label, e)

    _FILL_NAMES_JS = r"""(names) => {
      const [first, last] = names;
      const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
        return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
      const setVal = (el, val) => {
        el.focus();
        const proto = window.HTMLInputElement.prototype;
        const d = Object.getOwnPropertyDescriptor(proto, 'value');
        if (d && d.set) d.set.call(el, val); else el.value = val;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.blur();
      };
      // candidate name inputs: visible text inputs wide enough to be a name field (the language
      // selector at top-right is ~69px, so a >=120px width floor drops it).
      const inputs = [...document.querySelectorAll('input[type=text], input:not([type])')]
        .filter(vis).filter(e => e.getBoundingClientRect().width >= 120);
      const labelOf = (el) => { let n=el;
        for (let i=0;i<5 && n;i++){ const t=(n.textContent||'').toLowerCase();
          if (t.includes('first name')) return 'first';
          if (t.includes('last name'))  return 'last';
          n=n.parentElement; } return ''; };
      let fEl=null, lEl=null;
      for (const el of inputs){ const k=labelOf(el);
        if (k==='first' && !fEl) fEl=el; if (k==='last' && !lEl) lEl=el; }
      if (!fEl || !lEl){   // geometry fallback: leftmost wide input = First, next = Last
        const sorted = inputs.slice().sort((a,b)=>a.getBoundingClientRect().x-b.getBoundingClientRect().x);
        if (!fEl) fEl = sorted[0] || null;
        if (!lEl) lEl = sorted.find(e => e!==fEl) || null;
      }
      let done=0;
      if (fEl){ setVal(fEl, first); done++; }
      if (lEl && lEl!==fEl){ setVal(lEl, last); done++; }
      return done;
    }"""

    async def _fill_names(self, page, first: str, last: str) -> int:
        try:
            return await page.evaluate(self._FILL_NAMES_JS, [first, last])
        except Exception:
            return 0

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

    async def _confirm_submit(self, page) -> bool:
        """Click the CONFIRM control of a "submit your response / cannot be edited" modal — Submit /
        Confirm / Proceed (NEVER Stay / Cancel / Close-X). Hallo's is a styled text button ("Submit"),
        so use robust Playwright role/text locators (real click + actionability) before a JS fallback."""
        # 1) role=button by exact name
        for rx in ("Submit", "Confirm", "Proceed"):
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{rx}$", re.I))
                if await b.count():
                    el = b.first
                    if await el.is_visible():
                        await el.click(timeout=2500)
                        return True
            except Exception:
                pass
        # 2) any element with exact visible text (a styled span/link Submit)
        for rx in ("Submit", "Confirm", "Proceed"):
            try:
                t = page.get_by_text(re.compile(rf"^{rx}$", re.I), exact=False)
                for i in range(min(await t.count(), 4)):
                    el = t.nth(i)
                    try:
                        if await el.is_visible():
                            await el.click(timeout=2000)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
        # 3) JS fallback: click a visible element whose OWN trimmed text is exactly Submit/Confirm
        try:
            ok = await page.evaluate(r"""() => {
              const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
                return r.width>1 && r.height>1 && s.visibility!=='hidden' && s.display!=='none'; };
              const want = /^(submit|confirm|proceed)$/i;
              const bad  = /stay|cancel|\bback\b|\bedit\b|\bno\b|review|close/i;
              const pool = [...document.querySelectorAll('button,[role=button],a,span,div')].filter(
                e => vis(e) && e.children.length <= 1);
              for (const e of pool) { const t=(e.innerText||'').trim();
                if (want.test(t) && !bad.test(t)) { e.click(); return true; } }
              return false;
            }""")
            return bool(ok)
        except Exception:
            return False

    async def _ok_modal(self, page) -> bool:
        """Dismiss a transient proctoring/info modal by its OK/Got-it/Close/Try-Again button — NEVER
        'Appeal' (an integrity-appeal action) and NEVER the main disabled 'Continue'. Returns True iff
        it clicked something."""
        for name in ("OK", "Okay", "Got it", "Got It", "Dismiss", "Close", "Try Again", "Retake"):
            if await self._click(page, name, 1200):
                return True
        return False

    async def _cancel_appeal(self, page, body: str = None) -> bool:
        """The proctor "Appeal" modal ("Please provide any information that will be helpful when
        reviewing your account", APPEAL / CANCEL) pops up during the battery (e.g. a best/worst SJT
        page's proctor flag or an errant click on a latent Appeal control) and BLOCKS the page — nothing
        in the generic dismisser clears it. Close it via CANCEL (NEVER APPEAL — that files an integrity
        appeal against the account). Returns True iff the Appeal modal was present + cancelled."""
        if body is None:
            try:
                body = (await page.inner_text("body", timeout=1500)).lower()
            except Exception:
                body = ""
        if "reviewing your account" not in body and "helpful when reviewing" not in body:
            return False
        for rx in ("Cancel", "Close", "Dismiss"):
            if await self._click(page, rx, 1500):
                await page.wait_for_timeout(700)
                logger.info("[hallo] proctor Appeal modal dismissed (Cancel)")
                return True
        return False

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
                "() => { "
                # scroll EVERY inner scrollable container to the bottom (the questions/submit live in
                # an inner div, not the window — window.scrollTo misses them)
                "for (const e of document.querySelectorAll('*')) { "
                "  try { if (e.scrollHeight > e.clientHeight + 4) e.scrollTop = e.scrollHeight; } catch(_){} } "
                "const t=[]; "
                # buttons + links + role=button + ANY element that looks clickable (onclick / cursor:pointer)
                "const sel='button,[role=button],a,input,[onclick],[tabindex]'; "
                "for (const b of document.querySelectorAll(sel)) { "
                "  let cur=''; try { cur=getComputedStyle(b).cursor; } catch(_){} "
                "  const clickable = b.tagName==='BUTTON'||b.tagName==='A'||b.getAttribute('role')==='button'"
                "||b.onclick||cur==='pointer'||b.tagName==='INPUT'; "
                "  if(!clickable) continue; "
                "  const s=(b.tagName+':'+(b.type||'')+'|'+(b.innerText||b.value||'')+'|'"
                "+(b.getAttribute('aria-label')||'')).replace(/\\s+/g,' ').trim().slice(0,60); "
                "  if (s.replace(/[|:]/g,'').length>1) t.push(s+(b.disabled?' [x]':'')); } "
                "const au=document.querySelectorAll('audio,video').length; "
                "return {btns:t.slice(0,50), media:au}; }")
            logger.info("[hallo] device-check controls: %s | media_els=%s",
                        ctrls.get("btns"), ctrls.get("media"))
            # dump the outerHTML of every EMPTY (icon-only) button so we can identify the forward
            # (arrow/next) vs the audio controls precisely, by SVG/class.
            try:
                empties = await page.evaluate(
                    "() => [...document.querySelectorAll('button')].filter(b=>!((b.innerText||'')"
                    "+(b.getAttribute('aria-label')||'')).trim() && !b.disabled).map(b=>{"
                    "const r=b.getBoundingClientRect(); return b.outerHTML.slice(0,200)+' @['"
                    "+Math.round(r.x)+','+Math.round(r.y)+' '+Math.round(r.width)+'x'+Math.round(r.height)+']';}).slice(0,8)")
                logger.info("[hallo] icon-button HTML: %s", empties)
            except Exception as _he:
                logger.info("[hallo] icon-button HTML dump failed: %s", _he)
            # also capture a SCREENSHOT — a bare button label (e.g. '5' on the comprehension page) isn't
            # enough to see the real forward widget; the shot makes the layout obvious.
            try:
                from backend.tools.assessment_harvester import media as _media
                shot = await _media.capture(page, self.platform, "DEVCHECK", [], page.url)
                if shot:
                    logger.info("[hallo] device-check screenshot -> %s", shot)
            except Exception as _se:
                logger.info("[hallo] device-check screenshot failed: %s", _se)
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
        await page.wait_for_timeout(2000)
        await self._click(page, "Accept")            # cookie consent (may render late)
        # (1) NAME GATE — the tp-global-us welcome page renders First/Last Name as BARE MUI inputs with
        # NO id / name / placeholder / associated <label> (the visible "First Name" text is a MUI
        # floating label NOT wired to the input), so get_by_placeholder/get_by_label BOTH miss and the
        # old `:not([value="en"])` CSS fallback (a static-attribute selector) failed to exclude the
        # React-set language field → the surname landed in First Name and Last Name stayed EMPTY (proven
        # on every stuck-drive welcome screenshot). Fill in JS: pick the visible, reasonably-wide text
        # inputs (excludes the tiny top-right 'en' language selector), map First/Last by the nearest
        # floating-label text with a left→right geometry fallback, and set the value via the native
        # setter + input/change events so React registers it. The React SPA shows a LOADING SPINNER for
        # 5-10s+ before the form paints, so POLL for the inputs (a fixed sleep filled 0 into an empty
        # spinner page) — dismiss a late cookie banner each pass and stop once both names are set.
        filled = 0
        for _ in range(20):                          # up to ~40s for the form to render
            await page.wait_for_timeout(2000)
            await self._click(page, "Accept")
            filled = await self._fill_names(page, first, last)
            if filled >= 2:
                break
        logger.info("[hallo] name gate: filled=%s (%r %r)", filled, first, last)
        await self._snap(page, "WELCOME")
        await self._tick_all(page)
        # (1b) WELCOME → click "Begin Assessment" (tp-global-us) or fall back to a multi-page variant's
        # Continue/honor-code. Begin leads to the DEVICE-CHECK gate (NOT straight into the battery — the
        # old "no device-check page" assumption was WRONG; live 2026-09-19 the mic/camera gate always
        # follows Begin), so DON'T return here — pass the gate in (2).
        await page.wait_for_timeout(800)
        began = await self._click(page, "Begin Assessment")
        if not began:
            await self._click(page, "Continue")
            await page.wait_for_timeout(3500)
            await self._tick_all(page)               # older variant: an honor-code page
            await self._click(page, "Continue")
        await page.wait_for_timeout(5000)
        await self._snap(page, "AFTER_BEGIN")
        # (2) DEVICE CHECK — pass the "Check microphone and camera" gate, then the battery starts.
        await self._device_check(page)
        await self._snap(page, "AFTER_DEVCHECK")
        await page.wait_for_timeout(3000)

    async def _device_check(self, page) -> bool:
        """Pass the tp-global-us "Check microphone and camera" gate that follows Begin Assessment.
        Live-mapped 2026-09-19: the real v4l2loopback camera is auto-detected (green ✓); the MIC
        RECORDING TEST auto-records + plays back the moment "Start" is clicked (a "Retry" button
        appearing == the sample is captured — there is NO separate Stop/Play-back to click); then
        ticking the single "TP ... may use my recordings and AI for evaluation and proctoring" consent
        checkbox + the internet test finishing enables Continue. Returns True once Continue is clicked
        (into the battery); False (with a diagnostic dump) if it stayed walled."""
        # POLL for the device-check page to appear — "Begin Assessment" shows a loading spinner and can
        # take 10-40s to render the "Check microphone and camera" gate (esp. the 6-part variant), so a
        # single read misses it. Also bail early if we've clearly RESUMED straight into the battery (a
        # deep-resume token can skip the device check) so the core drives it.
        _DEV = ("check microphone and camera", "recording test", "won't be able to proceed",
                "confirm your voice")
        _BATTERY = ("question 1 of", "question 2 of", "question 3 of", "time left",
                    "start questionnaire", "start part", "answer each question", "write your notes",
                    "listen carefully")
        body = ""
        for _ in range(22):                          # up to ~44s
            try:
                body = (await page.inner_text("body", timeout=2000)).lower()
            except Exception:
                body = ""
            await self._cancel_appeal(page, body)
            if any(s in body for s in ("no longer accessible", "has either been completed or has expired",
                                       "assessment has expired", "already been completed")):
                logger.info("[hallo] token EXPIRED / no longer accessible — bailing")
                return False                         # wall() will report 'expired'
            if any(s in body for s in _DEV):
                break
            if any(s in body for s in _BATTERY):
                logger.info("[hallo] device-check: already in battery — core will drive")
                return False
            await page.wait_for_timeout(2000)
        else:
            logger.info("[hallo] device-check page never appeared (body=%r)", body[:80])
            return False                             # not the device-check page
        self._start_mic_feed()                       # gapless voice into the virtmic for the sample
        try:
            await page.wait_for_timeout(2500)
            # (a) mic recording test: Start → AUTO record+playback → a "Retry" button == captured.
            # A transient proctoring "OK/Appeal" modal (or a Start click that raced the render) can
            # leave "Start" showing and NO Retry → dismiss the modal (OK, NEVER Appeal) and re-click
            # Start until Retry appears. ~4 attempts × ~12s.
            got_retry = False
            for _attempt in range(4):
                await self._ok_modal(page)           # clear an OK/Appeal proctor modal if present
                await self._click(page, "Start", 2500)
                for _ in range(8):                   # up to ~12s per attempt
                    await page.wait_for_timeout(1500)
                    try:
                        got_retry = await page.evaluate(
                            "() => [...document.querySelectorAll('button')].some("
                            "b => /^retry$/i.test((b.innerText||'').trim()))")
                    except Exception:
                        got_retry = False
                    if got_retry:
                        break
                    await self._ok_modal(page)
                if got_retry:
                    break
            logger.info("[hallo] device-check recording test: retry_seen=%s", got_retry)
            # (b) tick the recording/AI proctoring consent
            await self._tick_all(page)
            # (c) wait for the internet test + Continue to enable, then click it into the battery
            for _ in range(30):                      # up to ~60s ("may take a few minutes")
                await self._tick_all(page)
                if await self._continue_enabled(page):
                    await self._click(page, "Continue", 3000)
                    await page.wait_for_timeout(3000)
                    logger.info("[hallo] device-check PASSED — entering battery")
                    return True
                await page.wait_for_timeout(2000)
            await self._log_devcheck_controls(page)  # still walled — diagnose
        finally:
            self._stop_mic_feed()
        return False

    # Hallo gates each module behind an instructions page with a module-START button ("Start Part 1",
    # "Start Questionnaire", "Begin", "I'm Ready") that the generic forward matcher misses — click it.
    _FWD = ("Start Part", "Start Questionnaire", "Begin", "I'?m Ready", "Ready to", "Start Now",
            "Start", "Continue", "Next", "Proceed", "Got it", "Resume", "OK", "I understand")

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

    # Buttons that must NEVER be auto-clicked when self-finding a forward (destructive / restart).
    _BTN_DENY = ("quit", "cookie", "skip to main", "retry", "log out", "logout", "appeal",
                 "cancel", "back", "restart", "exit", "report")

    async def _try_next_button(self, page) -> bool:
        """When genuinely stuck, click the NEXT enabled, visible, non-destructive button (cycling one
        per call via _btn_idx) to self-find an icon-only forward without knowing its label. Audio
        controls are harmless; the real forward advances. Also dismisses an OK/Continue re-entry dialog."""
        try:
            btns = page.locator("button:enabled")
            cands = []
            for i in range(min(await btns.count(), 30)):
                b = btns.nth(i)
                try:
                    if not await b.is_visible():
                        continue
                    lab = (((await b.inner_text()) or "") + " "
                           + ((await b.get_attribute("aria-label")) or "")).strip().lower()
                except Exception:
                    continue
                if any(d in lab for d in self._BTN_DENY):
                    continue
                cands.append((lab or "<icon>", b))
            if not cands:
                return False
            idx = getattr(self, "_btn_idx", 0) % len(cands)
            self._btn_idx = idx + 1
            lab, b = cands[idx]
            logger.info("[hallo] stuck — trying button %d/%d: %r", idx + 1, len(cands), lab[:30])
            await b.click(timeout=1500)
            return True
        except Exception:
            return False

    # COGNITIVE image-choice module (2026-09-15, live silas run stuck at step 82): a NON-VERBAL
    # reasoning item ("Five figures are shown. Four share a common rule ... choose the figure that does
    # not follow the same rule", "Q.1/10", a per-section countdown). The 5 answer choices are clickable
    # IMAGE CARDS (an <svg>/<img> in a MUI box), NOT text/radio options, so the core reads 0 options and
    # the page's Next stays DISABLED -> the harvest gave up ("no options/forward"). Detect + click a
    # choice card (which enables Next). Completion needs only Next to enable; a wrong pick still advances
    # (the section is SCORED, not gated).
    _COG_FIG_RE = re.compile(
        r"figures?\s+are\s+shown|share\s+a\s+common\s+rule|does\s+not\s+follow\s+the\s+same\s+rule"
        r"|which\s+figure|choose\s+the\s+figure", re.I)
    # A TIMED question section ("Q.1/10" + "time left") — cognitive / hardskill. NB the slash form
    # "Q.N/M" is distinct from the listening module's "Question k of M" (no slash), so this never
    # matches a listening passage.
    _TIMED_Q = re.compile(r"q\.?\s*\d+\s*/\s*\d+", re.I)
    # BEST/WORST SJT (hardskill "Part N - Question k of M"): a scenario + options A..D, each with a
    # thumbs-UP (best) + thumbs-DOWN (worst) icon; Next enables once ONE best + ONE (different) worst
    # are picked. The icons aren't text/radio options, so the core reads 0 options -> stuck.
    _BEST_WORST_RE = re.compile(r"best.{0,12}worst", re.I)

    async def _pick_figure_and_next(self, page) -> bool:
        """On a cognitive IMAGE-choice page, click one answer card to enable the disabled Next, then
        advance. SELF-CORRECTING: tries candidate cards until Next actually enables (so clicking a
        decorative icon by mistake is harmless — only the real choice unblocks Next). Best-effort pick
        (any valid card completes the item); never raises. Returns True iff it answered + moved on."""
        try:
            n = await page.evaluate(
                """() => {
                  const vis = e => { const r=e.getBoundingClientRect(); const ar=r.width/(r.height||1);
                    return r.width>=50 && r.height>=50 && r.width<=440 && r.height<=440
                      && ar>0.45 && ar<2.2 && r.bottom>0 && r.top<innerHeight; };
                  // an answer card = the smallest card-sized box wrapping exactly one svg/img choice.
                  const media=[...document.querySelectorAll('svg,img')];
                  const cards=[];
                  for (const m of media){
                    let el=m;
                    for (let i=0;i<5 && el.parentElement;i++){
                      const r=el.getBoundingClientRect();
                      if (r.width>=70 && r.height>=70) break;
                      el=el.parentElement;
                    }
                    if (el && vis(el) && !cards.includes(el)) cards.push(el);
                  }
                  if (cards.length<3 || cards.length>8){ window.__cogCards=null; return cards.length; }
                  window.__cogCards=cards;
                  return cards.length;
                }""")
        except Exception:
            return False
        if not n or n < 3 or n > 8:
            return False
        start = getattr(self, "_cog_pick", 0) % n
        self._cog_pick = start + 1
        order = list(range(start, n)) + list(range(0, start))
        for idx in order:
            try:
                await page.evaluate(
                    "(i) => { const c=window.__cogCards; if(!c||!c[i]) return; let el=c[i];"
                    " for(let k=0;k<4 && el;k++){ const cs=getComputedStyle(el);"
                    " if(el.tagName==='BUTTON'||el.getAttribute('role')==='button'||el.onclick"
                    "||cs.cursor==='pointer') break; el=el.parentElement; } (el||c[i]).click(); }", idx)
            except Exception:
                continue
            await page.wait_for_timeout(500)
            try:
                nxt = await page.evaluate(
                    "() => { const b=[...document.querySelectorAll('button')]"
                    ".find(x=>/^(next|submit)$/i.test((x.innerText||'').trim())); return b?!b.disabled:false; }")
            except Exception:
                nxt = False
            if nxt:
                logger.info("[hallo] cognitive figure: card %d/%d enabled Next", idx + 1, n)
                if await self._click(page, "Next", 1500) or await self._click(page, "Submit", 1500):
                    await page.wait_for_timeout(900)
                    return True
        return False

    async def _pick_best_worst_and_next(self, page) -> bool:
        """Sales/Hardskill best-worst SJT: click ONE option's thumbs-UP (best) + a DIFFERENT option's
        thumbs-DOWN (worst) to enable Next, then advance. LIVE-MAPPED 2026-09-19: each option is a
        `<tr>` — a text `<td>` then TWO `<td>` each holding an `<svg class="hoverElement">` (left=up/
        best, right=down/worst). The thumbs MUST be clicked with a REAL mouse click at the svg centre:
        a JS `el.click()` does NOT register the selection in React AND intermittently trips the proctor
        "Appeal" modal (proven — the old cluster+JS-click handler never enabled Next and kept opening
        Appeal). `page.mouse.click(x,y)` selects cleanly (A-up + B-down → Next enabled, no Appeal).
        SELF-CORRECTING across (best,worst) row pairs; best-effort; never raises."""
        try:
            rows = await page.evaluate(r"""() => {
              const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
                return r.width>1 && r.height>1 && s.visibility!=='hidden' && s.display!=='none'; };
              const svgs = [...document.querySelectorAll('svg.hoverElement, svg[class*="hover" i]')].filter(vis);
              const trs = [...document.querySelectorAll('tr')];
              const groups = new Map();
              for (const s of svgs) {
                const r = s.getBoundingClientRect();
                const tr = s.closest('tr');
                const key = tr ? ('tr' + trs.indexOf(tr)) : ('y' + Math.round(r.y / 20));
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push({x: r.x + r.width/2, y: r.y + r.height/2});
              }
              const out = [];
              for (const arr of groups.values()) {
                if (arr.length < 2) continue;               // an option row = up + down thumb
                arr.sort((a,b) => a.x - b.x);
                out.push({up:  [Math.round(arr[0].x), Math.round(arr[0].y)],
                          down:[Math.round(arr[arr.length-1].x), Math.round(arr[arr.length-1].y)]});
              }
              out.sort((a,b) => a.up[1] - b.up[1]);          // top→bottom
              return out;
            }""")
        except Exception:
            rows = None
        n = len(rows) if rows else 0
        if n < 2:
            return False
        pairs = [(b, w) for b in range(n) for w in range(n) if b != w]
        start = getattr(self, "_bw_pick", 0) % len(pairs)
        self._bw_pick = start + 1
        order = pairs[start:] + pairs[:start]
        for bi, wi in order[:max(n, 6)]:
            try:
                await page.mouse.click(rows[bi]["up"][0], rows[bi]["up"][1])
                await page.wait_for_timeout(300)
                await page.mouse.click(rows[wi]["down"][0], rows[wi]["down"][1])
                await page.wait_for_timeout(400)
            except Exception:
                continue
            try:
                nxt = await page.evaluate(
                    "() => { const b=[...document.querySelectorAll('button')]"
                    ".find(x=>/^(next|submit|finish)$/i.test((x.innerText||'').trim())); return b?!b.disabled:false; }")
            except Exception:
                nxt = False
            if nxt:
                logger.info("[hallo] sales best/worst: up-row%d down-row%d enabled Next", bi, wi)
                if (await self._click(page, "Next", 1500) or await self._click(page, "Submit", 1500)
                        or await self._click(page, "Finish", 1500)):
                    await page.wait_for_timeout(900)
                    return True
        return False

    async def advance(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=2000)).lower()
        except Exception:
            body = ""
        # proctor "Appeal" modal blocking the page — cancel it (never Appeal)
        if await self._cancel_appeal(page, body):
            self._stuck_advances = 0
            return True
        # END-OF-MODULE SUBMIT CONFIRMATION modal (live 2026-09-19): the LAST question of a paginated
        # module (e.g. listening Q5 of 5) carries a "Finish" button, not "Next"; clicking it opens
        # "Are you sure you wish to proceed with submitting your response? Once submitted, it cannot be
        # edited." with Stay / Submit. The generic forward re-clicked Finish (covered by the modal) and
        # never committed → the core looped re-reading Q5. Commit it: click the modal's Submit/Confirm
        # (NEVER Stay). Fires whenever the confirm text is present so it also commits every later module.
        if any(s in body for s in ("cannot be edited", "wish to proceed with submitting",
                                   "sure you wish to proceed", "proceed with submitting")):
            if await self._confirm_submit(page):
                self._stuck_advances = 0
                await page.wait_for_timeout(1800)
                return True
        # PAGINATED Q-of-N set (listening comprehension) whose "Finish" is DISABLED = an earlier
        # question is unanswered — a RESUME artifact (Hallo keeps your position but drops the selected
        # radios across sessions), so the last page shows Finish[disabled]. REWIND all the way to the
        # first question (click Back to the start) so the core answers the whole set forward; once all
        # are answered Finish enables + the confirm modal commits. Capped so a set where answers won't
        # stick falls through to the out-wait (the module timer auto-submits). Only triggers when a
        # DISABLED Finish + a Back are both present (never on a normal Next page).
        try:
            fin = await page.evaluate(
                "() => { const b=[...document.querySelectorAll('button')].find("
                "x=>/^finish$/i.test((x.innerText||'').trim()));"
                " const bk=[...document.querySelectorAll('button')].find("
                "x=>/^back$/i.test((x.innerText||'').trim()) && !x.disabled);"
                " return {finDisabled: b? b.disabled : null, back: !!bk}; }")
        except Exception:
            fin = {"finDisabled": None, "back": False}
        if fin.get("finDisabled") and fin.get("back"):
            self._rewinds = getattr(self, "_rewinds", 0) + 1
            if self._rewinds <= 4:
                logger.info("[hallo] listening Finish disabled — rewinding to Q1 (rewind #%d)", self._rewinds)
                for _ in range(8):                   # click Back to the first question
                    if not await self._click(page, "Back", 1200):
                        break
                    await page.wait_for_timeout(600)
                self._stuck_advances = 0
                await page.wait_for_timeout(800)
                return True
            # answers aren't sticking — out-wait the module timer (it auto-submits at 0:00)
            if "time left" in body:
                await page.wait_for_timeout(4000)
                return True
        # TIMED custom-widget sections the core can't read as options (Next stays disabled -> the
        # harvest gives up). Handle the known widgets, else WAIT OUT the countdown so the scored section
        # auto-advances at 0:00 (an unanswered section still auto-submits + moves on). Gated on the
        # widget REs so listening ("Question k of M" + a prep pad, is_prep below) is left untouched.
        #   (a) best/worst SJT thumbs (hardskill "Part N - Question k of M")
        #   (b) cognitive figure odd-one-out ("Q.1/10 ... time left")
        if "time left" in body and (self._BEST_WORST_RE.search(body) or self._COG_FIG_RE.search(body)):
            picked = False
            if self._BEST_WORST_RE.search(body):
                picked = await self._pick_best_worst_and_next(page)
            if not picked and self._COG_FIG_RE.search(body):
                picked = await self._pick_figure_and_next(page)
            if picked:
                self._stuck_advances = 0
                self._timed_waits = 0
                return True
            # known widget we couldn't click -> out-wait the countdown (bounded; resets per question)
            m = self._TIMED_Q.search(body) or re.search(r"question\s+\d+\s+of\s+\d+", body)
            qsig = m.group(0) if m else body[:40]
            if qsig != getattr(self, "_last_timed_sig", None):
                self._last_timed_sig = qsig
                self._timed_waits = 0
            self._timed_waits = getattr(self, "_timed_waits", 0) + 1
            if self._timed_waits <= 120:         # ~8 min > any single section countdown
                await page.wait_for_timeout(4000)
                return True
            self._timed_waits = 0
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
            # Distinguish a NORMAL long prep/listen countdown (a speaking "Prepare your response" timer
            # runs 30-60s — do NOT interrupt it) from a REAL stuck (the comprehension last-question that
            # keeps the "write your notes" pad → is_prep, answered but never submitted). Only after ~40s
            # on the SAME progress marker try to BREAK OUT once: scroll to reveal a below-fold submit,
            # re-try submit/next labels + a real Skip, press Enter, and dump controls+screenshot to
            # diagnose. (Earlier the dump fired at 12s and false-flagged normal speaking preps.)
            if self._prep_waits == 10 and not getattr(self, "_prep_dumped", False):
                self._prep_dumped = True
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
                for rx in ("Submit Answers", "Submit", "Next", "Continue", "Finish", "Done"):
                    if await self._click(page, rx, timeout=1500):
                        self._prep_waits = 0
                        await page.wait_for_timeout(1200)
                        return True
                if await self._skip_forward(page):
                    self._prep_waits = 0
                    await page.wait_for_timeout(1200)
                    return True
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
                await self._log_devcheck_controls(page)
            # A listening comprehension shows "MM:SS time left" (5 min) and its LAST question (Q5 of 5)
            # has NO submit — it AUTO-advances when the module timer expires. So the wait budget must
            # outlast that timer: ~95×4s ≈ 380s (>5 min). Re-answering the same Q5 each cycle is a
            # harmless no-op that doesn't reset the timer; we simply out-wait it.
            # Self-find the forward: on a genuinely stuck comprehension page the real forward is an ICON
            # button (no text) among audio controls. After the labelled attempts fail, cycle through the
            # enabled NON-destructive buttons one-at-a-time (audio controls are harmless no-ops; the real
            # forward advances). This also dismisses a re-entry OK/Continue dialog on a re-driven invite.
            if self._prep_waits >= 12 and self._prep_waits % 3 == 0:
                if await self._try_next_button(page):
                    await page.wait_for_timeout(1500)
                    return True
            if self._prep_waits <= 95:
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

    async def dismiss_noise(self, page) -> bool:
        """The core calls this at the TOP of every walk iteration. Base dismiss_noise clicks
        Close/OK/Continue — which on the end-of-module SUBMIT-confirmation modal ("... cannot be
        edited", Stay / Submit / ✕) would click the ✕ and DROP the submit → the module never commits
        and the last question loops. So intercept that modal here and CONFIRM it (Submit); never let the
        base close it. Otherwise defer to the generic dismisser."""
        try:
            body = (await page.inner_text("body", timeout=1500)).lower()
        except Exception:
            body = ""
        # proctor "Appeal" modal (blocks the page) — cancel it first
        if await self._cancel_appeal(page, body):
            return True
        if any(s in body for s in ("cannot be edited", "wish to proceed with submitting",
                                   "sure you wish to proceed", "proceed with submitting")):
            if not getattr(self, "_confirm_dumped", False):
                self._confirm_dumped = True
                try:
                    els = await page.evaluate(
                        "() => [...document.querySelectorAll('button,[role=button],a,span')]"
                        ".filter(e=>{const r=e.getBoundingClientRect(); const s=getComputedStyle(e);"
                        " return r.width>1&&r.height>1&&s.visibility!=='hidden'&&s.display!=='none'"
                        " && (e.innerText||'').trim().length>0 && (e.innerText||'').trim().length<20;})"
                        ".slice(0,20).map(e=>e.tagName+':'+(e.getAttribute('role')||'')+':'"
                        "+(e.innerText||'').replace(/\\s+/g,' ').trim())")
                    logger.info("[hallo] confirm-modal clickables: %s", els)
                except Exception:
                    pass
            if await self._confirm_submit(page):
                logger.info("[hallo] submit-confirmation modal COMMITTED")
                await page.wait_for_timeout(1500)
                return True
            return False       # modal present but not yet committed — do NOT let base close it
        return await super().dismiss_noise(page)

    async def read_item(self, page) -> dict:
        item = await super().read_item(page)
        try:
            body = (await page.inner_text("body", timeout=1500)).lower()
        except Exception:
            body = ""
        # CONSENT / TERMS checkbox mid-flow ("By checking this box, I agree to the Terms of Service and
        # Recording ..."). The page also carries a mic element, so the generic reader classified it as a
        # SPEAKING item and the core churn-spoke the SAME prompt forever without ever ticking the box.
        # Tick it and return a bare landing item so the walk loop clicks Continue/Next.
        _q = ((item.get("question", "") or "") + " " + body).lower()
        if re.search(r"by checking this box|i agree to the terms|agree to the terms of service|"
                     r"consent to (the )?record", _q):
            try:
                await self._tick_all(page)
            except Exception:
                pass
            item = dict(item)
            item["options"] = []
            item["has_mic"] = item["has_textarea"] = item["has_audio"] = item["has_video"] = False
            item["qimgs"] = []
            return item
        # LISTENING module: a PERSISTENT "Write your notes here while listening" scratch textarea makes
        # the generic reader classify the whole page as a typing test → the core churn-types the notes.
        # Suppress that textarea when there are no real answerable options, so the page is a transition
        # (wait for the audio + the actual questions) rather than a typing item. Real MCQ options (the
        # comprehension questions that appear after/below the audio) are kept and answered normally.
        if item.get("has_textarea") and not item.get("options") and not item.get("has_mic"):
            if "write your notes" in body or "listen carefully to the content" in body:
                item["has_textarea"] = False
        # LISTENING audio-phase race (root-caused 2026-09-14): on "Listen carefully to the content" the
        # 5 comprehension questions are ALREADY in the DOM, but the page has NOT handed off to answering
        # — the audio is still playing and the page stays on "Question 1 of 5". read_item used to keep
        # the options and the core answered them DURING the audio, so the page never advanced (Q5 looped
        # forever). Suppress the options while an <audio> is still playing so the adapter WAITS
        # (advance/is_prep) for the passage to finish; once the audio has ENDED the questions are
        # returned and answered in the correct phase, where the page's own forward works.
        if item.get("options") and "listen carefully to the content" in body:
            try:
                audio_playing = await page.evaluate(
                    "() => { const a=[...document.querySelectorAll('audio')]; return a.length>0 && "
                    "a.some(x => !x.ended && (x.currentTime||0) < ((x.duration||1e9) - 0.3)); }")
            except Exception:
                audio_playing = False
            if audio_playing:
                item["options"] = []
        # Loop diagnostic: when the SAME question is read repeatedly (the last-of-comprehension Q5 that
        # never advances — a PAGINATED module: Q1..Q4 advance on Next, Q5 has a different forward), dump
        # THAT page's controls + a screenshot ONCE to reveal its real submit/forward control.
        q = (item.get("question") or "")[:60]
        if q and item.get("options") and q == getattr(self, "_last_read_q", None):
            # a real ANSWERABLE-question loop (Q5), not the audio-wait phase
            self._read_repeat = getattr(self, "_read_repeat", 0) + 1
            if self._read_repeat == 4 and not getattr(self, "_loop_dumped", False):
                self._loop_dumped = True
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
                await self._log_devcheck_controls(page)   # now scrolled to the Submit area
        else:
            self._last_read_q = q
            self._read_repeat = 0 if item.get("options") else getattr(self, "_read_repeat", 0)
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

    async def handle_writex(self, page, email: dict) -> bool:
        """Fill the WriteX email THEN advance the Hallo writing module. Hallo's submit is a Next/Submit
        or an icon-only button (same class as the comprehension Q5 forward), and the core's writing
        branch does NOT call advance() — so after typing, click the forward here: labelled first, then
        the real Skip, then cycle a non-destructive button (self-find, one per call — over a couple of
        core re-reads it lands the icon submit). Returns True so the core loop re-reads (a new item if
        we advanced; the same writing item — and the NEXT cycled button — if not)."""
        await super().handle_writex(page, email)          # types the drafted email into the textarea
        await page.wait_for_timeout(700)
        for rx in ("Submit Answer", "Submit", "Next", "Continue", "Finish", "Complete", "Done"):
            if await self._click(page, rx, timeout=1500):
                await page.wait_for_timeout(1000)
                return True
        if await self._skip_forward(page):
            await page.wait_for_timeout(1000)
            return True
        await self._try_next_button(page)                 # cycle an icon button (self-find the submit)
        await page.wait_for_timeout(1000)
        return True

    async def handle_speaking(self, page, record_secs: float = 4.0, mic_say_wav=None) -> bool:
        """Hallo open-response (Speaking) items AUTO-RECORD ~60s (a red STOP button + a 'Recording will
        end in N seconds' countdown) and score the transcribed answer. Feed a spoken answer into the
        virtmic for the window, try to STOP early, then advance to the next question. Returns True when
        the recorder is done (the item advanced)."""
        self._start_mic_feed(mic_say_wav)
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

    async def is_done(self, page) -> bool:
        """Completion check + a one-shot DIAGNOSTIC: the FIRST time completion is seen, log the final
        body text + capture a screenshot, and flag whether a proctoring 'Appeal / reviewing your
        account' overlay is present. Lets the log distinguish a CLEAN unflagged completion from a
        submitted-but-flagged one (an integrity flag is a different ceiling than a clean pass)."""
        done = await super().is_done(page)
        if done and not getattr(self, "_done_logged", False):
            self._done_logged = True
            try:
                body = await page.inner_text("body", timeout=2000)
            except Exception:
                body = ""
            low = body.lower()
            flagged = any(s in low for s in ("appeal", "reviewing your account", "under review",
                                             "flagged", "integrity", "violation", "we detected"))
            logger.info("[hallo] COMPLETE — proctor_flagged=%s body=%r",
                        flagged, " ".join(body.split())[:400])
            try:
                from backend.tools.assessment_harvester import media as _media
                shot = await _media.capture(page, self.platform, "COMPLETE", [], page.url)
                if shot:
                    logger.info("[hallo] completion screenshot -> %s", shot)
            except Exception:
                pass
        return done

    async def wall(self, page) -> str | None:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            return None
        # TOKEN EXPIRED / already consumed — Hallo links are single-use + time-limited (~1 day). A dead
        # token pops "The assessment is no longer accessible as it has either been completed or has
        # expired." over the welcome page; Begin then never advances. Terminal — stop fast so the drain
        # marks it and moves on (do NOT keep re-clicking Begin for the whole session).
        if any(s in body for s in ("no longer accessible", "has either been completed or has expired",
                                   "assessment has expired", "already been completed")):
            return "expired"
        if "won't be able to proceed" in body and ("camera" in body or "microphone" in body):
            # only a WALL if we're stuck ON the device-check (Continue never enabled)
            if not await self._continue_enabled(page):
                return "device_check"
        if re.search(r"you have been (logged out|removed)|integrity (violation|check failed)", body):
            return "proctor"
        return None
