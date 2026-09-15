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
        """Best/worst SJT: click ONE option's thumbs-UP (best) + a DIFFERENT option's thumbs-DOWN
        (worst) to enable the disabled Next, then advance. The two thumb icons per option row are
        detected by CLUSTERING small clickable icon controls into rows (left icon = up/best, right =
        down/worst). SELF-CORRECTING: tries distinct (best,worst) row pairs until Next actually enables.
        Best-effort (any valid distinct pair completes the item); never raises."""
        try:
            info = await page.evaluate(
                """() => {
                  const vis=e=>{const r=e.getBoundingClientRect();
                    return r.width>=12 && r.width<=70 && r.height>=12 && r.height<=70
                      && r.bottom>0 && r.top<innerHeight;};
                  const all=[...document.querySelectorAll('button,[role=button],svg,[class*="thumb" i]')];
                  const seen=new Set(); const ics=[];
                  for (const n of all){
                    if(!vis(n)) continue;
                    const txt=((n.innerText||'')+(n.getAttribute('aria-label')||'')
                      +(n.getAttribute('title')||'')).trim().toLowerCase();
                    if(/back|next|quit|appeal|\\bok\\b|cookie|skip|close|stay|submit/.test(txt)) continue;
                    let clk=n;
                    for(let k=0;k<3 && clk;k++){ const cs=getComputedStyle(clk);
                      if(clk.tagName==='BUTTON'||clk.getAttribute('role')==='button'||clk.onclick
                        ||cs.cursor==='pointer') break; clk=clk.parentElement; }
                    clk=clk||n;
                    const rr=clk.getBoundingClientRect();
                    if(rr.width>90||rr.height>90) continue;
                    const key=Math.round(rr.x)+','+Math.round(rr.y);
                    if(seen.has(key)) continue; seen.add(key);
                    ics.push({el:clk, x:rr.x+rr.width/2, y:rr.y+rr.height/2});
                  }
                  ics.sort((a,b)=>a.y-b.y || a.x-b.x);
                  const rows=[];
                  for(const ic of ics){
                    let row=rows.find(R=>Math.abs(R.y-ic.y)<20);
                    if(!row){ row={y:ic.y, items:[]}; rows.push(row); }
                    row.items.push(ic);
                  }
                  const optRows=rows.filter(R=>R.items.length===2);
                  window.__bwRows=optRows.map(R=>R.items.sort((a,b)=>a.x-b.x).map(i=>i.el));
                  return {rows:optRows.length, icons:ics.length};
                }""")
        except Exception:
            return False
        rows = (info or {}).get("rows") or 0
        if rows < 2:
            # detector missed the thumb pairs -> dump the option-area icon DOM ONCE so the exact
            # up/down control structure can be targeted precisely on the next iteration.
            if not getattr(self, "_bw_dumped", False):
                self._bw_dumped = True
                try:
                    cands = await page.evaluate(
                        "() => [...document.querySelectorAll('button,[role=button],svg,[class*=\"thumb\" i],[class*=\"icon\" i]')]"
                        ".filter(e=>{const r=e.getBoundingClientRect(); return r.width>0 && r.width<=90 && r.height<=90 && r.top<innerHeight && r.bottom>0;})"
                        ".slice(0,28).map(e=>{const r=e.getBoundingClientRect();"
                        " const cn=(e.className&&e.className.baseVal!==undefined)?e.className.baseVal:(e.className||'');"
                        " return (e.tagName+'.'+String(cn)).slice(0,44)+' @['+Math.round(r.x)+','+Math.round(r.y)"
                        "+' '+Math.round(r.width)+'x'+Math.round(r.height)+'] al='+(e.getAttribute('aria-label')||'');})")
                    logger.info("[hallo] best/worst detector MISS (rows=%s) — icon candidates: %s",
                                rows, cands)
                    await self._log_devcheck_controls(page)
                except Exception as _e:
                    logger.info("[hallo] best/worst dump failed: %s", _e)
            return False
        allpairs = [(b, w) for b in range(rows) for w in range(rows) if b != w]
        start = getattr(self, "_bw_pick", 0) % len(allpairs)
        self._bw_pick = start + 1
        order = allpairs[start:] + allpairs[:start]
        for bi, wi in order[:max(rows, 4)]:
            try:
                await page.evaluate(
                    "([b,w]) => { const R=window.__bwRows; if(!R) return;"
                    " if(R[b]&&R[b][0]) R[b][0].click(); if(R[w]&&R[w][1]) R[w][1].click(); }", [bi, wi])
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
                logger.info("[hallo] best/worst SJT: best=row%d worst=row%d enabled Next", bi, wi)
                if await self._click(page, "Next", 1500) or await self._click(page, "Submit", 1500):
                    await page.wait_for_timeout(900)
                    return True
        return False

    async def advance(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=2000)).lower()
        except Exception:
            body = ""
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
        if "won't be able to proceed" in body and ("camera" in body or "microphone" in body):
            # only a WALL if we're stuck ON the device-check (Continue never enabled)
            if not await self._continue_enabled(page):
                return "device_check"
        if re.search(r"you have been (logged out|removed)|integrity (violation|check failed)", body):
            return "proctor"
        return None
