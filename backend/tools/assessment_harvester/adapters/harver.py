"""Harver adapter — TTEC's post-apply assessment. TTEC (Oracle Taleo, `teletech.taleo.net`) emails
"Your Application - Required Assessments" from `jobopportunities@ttec.com`; the link is a Taleo
`externalServiceController.jsp?sealedRequestId=...` that, once the candidate ACCEPTS the privacy
agreement and LOGS IN to their Taleo account, redirects (externalPopup.jsp → a 3s meta-refresh) to the
Harver assessment player at `st.harver.app/<code>`. So the entry is a Taleo->Harver HANDOFF that lives
here; the battery itself (situational-judgement best/worst, personality Likert, cognitive MCQ, typing)
is STRUCTURALLY the same shape the generic Adapter + the Hallo battery helpers already answer.

The Taleo candidate credentials were saved at APPLY time by `applier/strategies/taleo._save_taleo_account`
(`data/taleo_accounts.json`, keyed by the persona email) — `enter()` looks them up via
`taleo.taleo_account(mailbox)`. LIVE-VERIFIED 2026-09-17 (scratchpad ttec_drive.py, reese.hayes8494):
Privacy "I Accept" -> Taleo login (fields `dialogTemplate-dialogForm-login-name1`/`-password`, submit
`#dialogTemplate-dialogForm-login-defaultCmd`) -> `externalpopup/externalPopup.jsp?...&partnerEntityKey=
1010240` -> `<meta refresh 3s>` -> `https://st.harver.app/...`. 266 of 274 pending TTEC assessment
mailboxes have both saved creds AND a live token, so the handoff is reachable.

PHASE 1 (this file): a solid handoff + a best-effort Harver device-check pass, with a control/DOM dump on
every gate so the first live run CAPTURES Harver's real DOM. read_item/answer stay generic for the
capture; the module-specific selectors get refined from the captured DOM.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from .base import Adapter

logger = logging.getLogger("assessment_harvester")


class HarverAdapter(Adapter):
    platform = "harver"

    def __init__(self, mailbox: str = ""):
        # The persona email — used to look up the saved Taleo login (data/taleo_accounts.json) for the
        # handoff. harvest_runner passes the full `first.last<N>@takhet.com` address here.
        self.mailbox = mailbox
        self._mic_feed: subprocess.Popen | None = None

    # ------- account / creds -------
    def _account(self) -> dict | None:
        try:
            from backend.applier.strategies.taleo import taleo_account
            return taleo_account(self.mailbox)
        except Exception as exc:
            logger.info("[harver] taleo_account lookup failed for %s: %s", self.mailbox, exc)
            return None

    # ------- small helpers (mirrors Hallo) -------
    async def _click(self, page, rx: str, timeout: int = 3000) -> bool:
        try:
            b = page.get_by_role("button", name=re.compile("^" + rx, re.I)).first
            if await b.count() and await b.is_enabled():
                await b.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    async def _click_sel(self, page, selector: str, timeout: int = 3000) -> bool:
        try:
            el = page.locator(selector).first
            if await el.count() and await el.is_visible():
                await el.click(timeout=timeout)
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

    # Harver's forward CTA varies per gate/module: the Consent page = "Continue", the Session-Monitoring
    # notice = "Accept and Continue", module intros = "Start"/"Start Assessment"/"Begin"/"Get Started".
    # A cookie banner ("Ok, got it") overlays the first pages and must be dismissed (it is NOT progress).
    _FWD = ("Accept and Continue", "Start Assessment", "Start Test", "Start the", "Get Started",
            "I Agree", "Agree and Continue", "Agree", "I understand and", "I understand",
            "Continue", "Begin", "Proceed", "Next", "Start", "Submit")
    _FWD_DENY = ("decline", "log out", "logout", "skip to main", "cancel", "back", "help",
                 "open menu", "cookie", "quit", "exit")

    async def _dismiss_cookie(self, page) -> None:
        for rx in ("Ok, got it", "Accept all", "Accept cookies"):
            if await self._click(page, rx, timeout=1200):
                await page.wait_for_timeout(400)
                return

    async def _forward(self, page) -> bool:
        """Click Harver's real forward CTA (dismissing the cookie banner + playing any gating intro video
        first). Returns True only when a genuine forward (not the cookie/deny buttons) was clicked."""
        await self._dismiss_cookie(page)
        await self._play_gating_video(page)          # a Content-Page video must be watched before Continue works
        for rx in self._FWD:
            if await self._click(page, rx, timeout=2000):
                return True
        return False

    async def _continue_enabled(self, page) -> bool:
        try:
            return await page.evaluate(
                "() => { const c=[...document.querySelectorAll('button')]"
                ".find(b=>/^(continue|start|begin|next|proceed|i understand|agree|accept)/i"
                ".test((b.innerText||'').trim())); return c? !c.disabled : false; }")
        except Exception:
            return False

    async def _play_gating_video(self, page) -> bool:
        """Harver intro pages ("Before we get started…", module intros) gate the NEXT step behind actually
        WATCHING a <video> ("Please watch the full video to continue") — the Continue button LOOKS enabled
        but a click before the video ends is a no-op. So PLAY it muted at 16x to completion (real
        play/timeupdate/ended events, which is what Harver tracks). Returns True if a gating video existed."""
        try:
            info = await page.evaluate(
                "() => { const v=[...document.querySelectorAll('video')].find(x=>x.offsetParent!==null);"
                " if(!v) return {has:false};"
                " try{ v.muted=true; v.playbackRate=16; const p=v.play(); if(p&&p.catch)p.catch(()=>{}); }catch(e){}"
                " return {has:true, dur:v.duration, cur:v.currentTime}; }")
            if not info.get("has"):
                return False
            logger.info("[harver] gating video dur=%s — playing at 16x", info.get("dur"))
            for _ in range(14):                       # ~21s > a 94s clip at 16x (~6s) + slack
                await page.wait_for_timeout(1500)
                st = await page.evaluate(
                    "() => { const v=[...document.querySelectorAll('video')].find(x=>x.offsetParent!==null);"
                    " if(!v) return {gone:true};"
                    " try{ if(v.paused && !v.ended){ v.muted=true; v.playbackRate=16; const p=v.play(); if(p&&p.catch)p.catch(()=>{});} }catch(e){}"
                    " return {ended:v.ended, cur:v.currentTime, dur:v.duration}; }")
                if st.get("gone") or st.get("ended") or (st.get("dur") and st.get("cur", 0) >= st["dur"] - 0.6):
                    logger.info("[harver] gating video finished (cur=%s dur=%s)", st.get("cur"), st.get("dur"))
                    break
            await page.wait_for_timeout(800)
            return True
        except Exception as e:
            logger.info("[harver] video play error: %s", e)
            return False

    async def _dump_controls(self, page, tag: str, force_shot: bool = False) -> None:
        """Dump every clickable control + input + media element (+ a screenshot) so a captured run
        reveals Harver's real DOM (button labels, field names, iframes) to build the selectors from."""
        try:
            info = await page.evaluate(
                "() => {"
                " const btn=[...document.querySelectorAll('button,[role=button],a,input[type=submit],input[type=button]')]"
                "   .map(b=>((b.innerText||b.value||'')+'|'+(b.getAttribute('aria-label')||'')).replace(/\\s+/g,' ').trim())"
                "   .filter(s=>s.replace(/[|]/g,'').length>0).slice(0,40);"
                " const inp=[...document.querySelectorAll('input,select,textarea')]"
                "   .map(e=>e.tagName+':'+(e.type||'')+':'+(e.name||e.id||'')+(e.placeholder?('('+e.placeholder+')'):'')).slice(0,30);"
                " const fr=[...document.querySelectorAll('iframe')].map(f=>f.src).slice(0,10);"
                " const media={audio:document.querySelectorAll('audio').length, video:document.querySelectorAll('video').length};"
                " return {btn, inp, fr, media, url:location.href, title:document.title};"
                "}")
            logger.info("[harver] %s | url=%s title=%r", tag, info.get("url"), info.get("title"))
            logger.info("[harver] %s | buttons=%s", tag, info.get("btn"))
            logger.info("[harver] %s | inputs=%s", tag, info.get("inp"))
            logger.info("[harver] %s | iframes=%s media=%s", tag, info.get("fr"), info.get("media"))
        except Exception as e:
            logger.info("[harver] control dump (%s) failed: %s", tag, e)
        # The per-screen SCREENSHOT is a capture-time diagnostic (slow: render + disk). Skip it during a
        # completion run (HARVER_CAPTURE unset) so the battery isn't paced by screenshots; the button/input
        # LOG above still maps each screen. Banking (core._bank) is independent of this.
        if not (os.getenv("HARVER_CAPTURE") or force_shot) and tag not in ("COMPLETE", "handoff_stuck"):
            return
        try:
            from backend.tools.assessment_harvester import media as _media
            shot = await _media.capture(page, self.platform, tag, [], page.url)
            if shot:
                logger.info("[harver] %s screenshot -> %s", tag, shot)
        except Exception:
            pass

    def _start_mic_feed(self) -> None:
        """Gapless speech into the virtmic sink so a Harver mic check samples live voice (same trick as
        Hallo). Best-effort; a missing asset/mic is a no-op."""
        try:
            from backend.tools.assessment_harvester import assets, mic
            wav = (assets.ensure_assets() or {}).get("audio")
            if not wav or not os.path.exists(wav):
                return
            from backend.tools.assessment_harvester import camera as _cam
            self._mic_feed = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-re",
                 "-i", wav, "-f", "pulse", "-device", mic.SINK, "harver_devcheck"],
                env=mic._env(), preexec_fn=_cam.pdeathsig,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self._mic_feed = None

    def _stop_mic_feed(self) -> None:
        try:
            if self._mic_feed:
                self._mic_feed.kill()
        except Exception:
            pass
        self._mic_feed = None

    # ------- Taleo -> Harver handoff -------
    async def _taleo_handoff(self, page, url: str) -> bool:
        """Privacy Agreement -> Taleo candidate login (saved creds) -> wait for the meta-refresh to
        st.harver.app. Returns True once the URL is on harver.app."""
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        # (1) Privacy Agreement — "I Accept"
        if not await self._click_sel(page, 'input[value="I Accept"]'):
            await self._click_sel(page, 'input[value*="Accept" i]') or await self._click(page, "I Accept")
        await page.wait_for_timeout(3500)
        # (2) Taleo login with the saved candidate credentials
        acct = self._account()
        if not acct:
            logger.info("[harver] NO saved Taleo creds for %s — cannot log in to reach Harver", self.mailbox)
            await self._dump_controls(page, "no_creds")
            return "harver.app" in (page.url or "")
        try:
            if await page.locator('input[type=password]').first.count():
                await self._fill_sel(page, '#dialogTemplate-dialogForm-login-name1', acct.get("username", ""))
                await self._fill_sel(page, '#dialogTemplate-dialogForm-login-password', acct.get("password", ""))
                if not await self._click_sel(page, '#dialogTemplate-dialogForm-login-defaultCmd'):
                    await self._click_sel(page, 'input[value="Login"]') or await self._click(page, "Login")
        except Exception as exc:
            logger.info("[harver] login step error: %s", exc)
        # (3) wait for externalPopup.jsp + its 3s meta-refresh to st.harver.app
        for _ in range(16):
            await page.wait_for_timeout(2000)
            u = page.url or ""
            if "harver.app" in u or "harver.com" in u:
                logger.info("[harver] reached Harver: %s", u)
                return True
            # nudge: if we're parked on the externalPopup stub, follow its meta-refresh target
            if "externalPopup" in u or "externalServiceController" in u:
                try:
                    href = await page.evaluate(
                        "() => { const m=document.querySelector('meta[http-equiv=\"refresh\" i]');"
                        " if(!m) return null; const c=m.getAttribute('content')||'';"
                        " const i=c.toLowerCase().indexOf('url='); return i>=0? c.slice(i+4).trim() : null; }")
                    if href and "harver" in href:
                        await page.goto(href, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass
        logger.info("[harver] did NOT reach Harver, stuck at %s", page.url)
        await self._dump_controls(page, "handoff_stuck")
        return "harver.app" in (page.url or "")

    async def _fill_sel(self, page, selector: str, value: str) -> None:
        try:
            el = page.locator(selector).first
            if await el.count():
                await el.fill(value, timeout=4000)
        except Exception:
            pass

    async def enter(self, page, url: str) -> None:
        """Taleo -> Harver handoff ONLY. The main harvest loop then drives everything: advance() walks the
        consent / session-monitoring / camera-test / intro-video GATES (tick + play the gating video +
        click the Harver CTA), and read_item detects the SJT/personality/cognitive questions. (An in-enter
        gate loop used to spin, uselessly clicking Continue on the first UNANSWERED SJT question and
        leaving the page in a bad state before the main loop could answer it.)"""
        reached = await self._taleo_handoff(page, url)
        if not reached:
            return
        self._start_mic_feed()                 # gapless voice for any mic check; left running for the session
        await page.wait_for_timeout(3000)
        await self._dismiss_cookie(page)
        await self._dump_controls(page, "harver_landing")

    async def advance(self, page) -> bool:
        """Gate + module-to-module forward for the main harvest loop: tick any consent box, play a gating
        intro video, click the Harver CTA ("Continue" / "Accept and Continue" / "Start the assessment")."""
        await self._tick_all(page)
        if await self._forward(page):
            await page.wait_for_timeout(1500)
            return True
        return await super().advance(page)

    async def dismiss_noise(self, page) -> bool:
        """The base dismiss_noise clicks a bare "Continue"/"Accept" button — but on Harver's consent page
        that Continue is gated behind TWO checkboxes, so clicking it without ticking them is a no-op AND
        (returning True) makes the core loop `continue` forever, starving read_item/advance. Harver's only
        real "noise" is the cookie banner; dismiss THAT and return False so control passes to advance(),
        which ticks the consent boxes BEFORE clicking Continue."""
        await self._dismiss_cookie(page)
        # Camera-test "Image capture failed / reload your browser" is a TRANSIENT device read glitch —
        # it appears when several parallel lanes grab a still photo off the SHARED /dev/video0 at the
        # same instant (the page itself tells you to reload). Reload to re-run the capture instead of
        # letting core stall it as `stuck` (which would burn a perfectly good invite). Bounded so a
        # genuinely-dead camera still surfaces after a few tries.
        try:
            body = (await page.inner_text("body", timeout=1500)).lower()
        except Exception:
            body = ""
        if ("image capture failed" in body or "reload your browser" in body
                or "couldn't capture" in body or "unable to capture" in body):
            n = getattr(self, "_cam_retry", 0)
            if n < 4:
                self._cam_retry = n + 1
                logger.info("[harver] camera image-capture failed — reloading to retry (%d/4)", n + 1)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(2500)
                except Exception:
                    pass
                return True   # handled → core re-reads the reloaded camera test
        return False

    # ---- Situational-Judgement (rate-each) module ----
    # Layout: a scenario video (already played) + a prompt ("… What do you do?") + N response rows, each a
    # Best/Neutral/Worst segmented control; you mark ONE Best + ONE Worst (the rest stay Neutral) → Continue.
    _READ_SJT_JS = """() => {
      // Best/Neutral/Worst are NOT <button>s (they are role=button / styled divs) — find the INNERMOST
      // element whose exact text is the word, regardless of tag, so the reader/clicker actually match.
      const leaves=(word)=>{
        const all=[...document.querySelectorAll('*')].filter(e=>{
          const t=(e.innerText||e.textContent||'').trim().toLowerCase();
          const r=e.getBoundingClientRect();
          return t===word && r.width>0 && r.height>0; });
        return all.filter(e=>![...e.querySelectorAll('*')].some(c=>
          (c.innerText||c.textContent||'').trim().toLowerCase()===word));
      };
      const best=leaves('best'), worst=leaves('worst');
      if (best.length<2 || worst.length<2 || best.length!==worst.length)
        return {is_sjt:false, nb:best.length, nw:worst.length};
      // each response row = the smallest ancestor of a Best button that also contains its Neutral+Worst
      const rows=[];
      for (const bb of best){
        let el=bb;
        for (let i=0;i<7 && el.parentElement;i++){ el=el.parentElement;
          const t=(el.innerText||'');
          if(/best/i.test(t)&&/worst/i.test(t)&&/neutral/i.test(t)&&t.replace(/best|neutral|worst/gi,'').trim().length>10) break; }
        const raw=(el.innerText||'').replace(/\\bBest\\b|\\bNeutral\\b|\\bWorst\\b/gi,'').replace(/\\s+/g,' ').trim();
        rows.push(raw);
      }
      // scenario/prompt = visible text ABOVE the first response row, minus the info banner + headings
      let scenario='';
      try{
        const bodyT=document.body.innerText||'';
        const cut=rows[0]? bodyT.indexOf(rows[0].slice(0,25)) : -1;
        let head=cut>0? bodyT.slice(0,cut) : bodyT;
        head=head.replace(/Make sure to watch[^\\n]*/gi,'').replace(/What do you do\\??/gi,' ')
                 .replace(/Select the best and worst response/gi,' ')
                 .replace(/Skip to main content|Help|Log out|Open menu/gi,' ');
        const lines=head.split('\\n').map(s=>s.trim()).filter(s=>s.length>25);
        scenario=(lines.pop()||'').trim();  // the last substantial line above the options = the scenario
      }catch(e){}
      return {is_sjt:true, scenario, responses:rows, n:best.length};
    }"""

    # ---- Personality Questionnaire (bipolar 6-point forced choice) ----
    # Layout: a stem ("When I see that someone is upset, I…") + a LEFT pole phrase + 6 clickable circles
    # (innerText 1..6, aria-label -3..+3) + a RIGHT pole phrase; clicking a circle auto-saves + advances.
    _READ_PERS_JS = """() => {
      // the 6 scale circles can be ANY tag (button / a / label / div / role=radio) and the '1'..'6' may be
      // the innerText, the aria-label, the value, OR the -3..+3 aria — so match broadly by leaf element.
      const val=e=>{
        // Harver circles are <button type=submit> with EMPTY innerText, value="1".."6" (the clean scale),
        // and aria-label="-3..-1,1,2,3". Prefer VALUE (1-6); the aria "1"/"2"/"3" would collide with the
        // low scale points, so it is only the LAST resort via the -3..3 mapping.
        const vv=((e.getAttribute&&e.getAttribute('value'))||'').trim();
        if(/^[1-6]$/.test(vv)) return +vv;
        const dv=((e.getAttribute&&e.getAttribute('data-value'))||'').trim();
        if(/^[1-6]$/.test(dv)) return +dv;
        const t=(e.innerText||e.textContent||'').trim();
        if(/^[1-6]$/.test(t)) return +t;
        const a=((e.getAttribute&&e.getAttribute('aria-label'))||'').trim();
        const m=a.match(/^(-?[1-3])$/); if(m){ const n=+m[1]; return n<0? n+4 : n+3; } // -3..3 -> 1..6
        return null; };
      const cells=[...document.querySelectorAll('button,[role=button],[role=radio],a,label,div,span')]
        .map(e=>({e, v:val(e), r:e.getBoundingClientRect()}))
        .filter(o=>o.v!==null && o.r.width>0 && o.r.height>0 && o.r.width<90 && o.r.height<90);
      // keep leaves (no descendant is also a cell) + dedup by position
      const seen=new Set(); const digitsAll=[];
      for(const o of cells){ if([...o.e.querySelectorAll('*')].some(c=>val(c)!==null)) continue;
        const k=Math.round(o.r.x)+','+Math.round(o.r.y); if(seen.has(k))continue; seen.add(k); digitsAll.push(o); }
      const digits=digitsAll.map(o=>o.e);
      const body=(document.body.innerText||'');
      // A section-end TRANSITION ("Assessment completed" / "Section complete") keeps the last item's
      // circles in the DOM but is NOT an answerable item — bail so the loop advances to the next module.
      if(/assessment completed|section complete|you have completed|module complete|thanks? for completing/i.test(body))
        return {is_pers:false, transition:true};
      const isPers=/select your preferred answer from the scale|saves and advances automatically/i.test(body);
      if (digits.length<5){
        // diagnostic sample so we can see EXACTLY what the circles are on the next run
        const dbg=[...document.querySelectorAll('button,[role=button],[role=radio],a,label')]
          .filter(e=>{const r=e.getBoundingClientRect(); return r.width>0&&r.width<90&&r.height<90;})
          .slice(0,14).map(e=>{const r=e.getBoundingClientRect(); return (e.tagName+'/'+(e.getAttribute('role')||'')
            +' it='+JSON.stringify((e.innerText||'').trim().slice(0,6))
            +' al='+((e.getAttribute('aria-label')||'').slice(0,6))
            +' @'+Math.round(r.x)+','+Math.round(r.y));});
        return {is_pers:false, ns:digits.length, isPers, dbg};
      }
      digits.sort((a,b)=>a.getBoundingClientRect().x-b.getBoundingClientRect().x);
      const first=digits[0]&&digits[0].getBoundingClientRect();
      const last=digits[digits.length-1]&&digits[digits.length-1].getBoundingClientRect();
      let left='', right='', stem='';
      if(first&&last){
        const rowY=first.y+first.height/2;
        const cand=[...document.querySelectorAll('p,span,div,label')].filter(e=>{
          const t=(e.innerText||'').trim(); const r=e.getBoundingClientRect();
          return t.length>2 && t.length<140 && r.width>0 && r.width<420
            && Math.abs((r.y+r.height/2)-rowY)<70
            && ![...e.querySelectorAll('*')].some(c=>(c.innerText||'').trim()===t); });
        const L=cand.filter(e=>e.getBoundingClientRect().right<=first.x+8)
                    .sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right);
        const R=cand.filter(e=>e.getBoundingClientRect().left>=last.right-8)
                    .sort((a,b)=>a.getBoundingClientRect().left-b.getBoundingClientRect().left);
        left=L[0]?(L[0].innerText||'').trim():''; right=R[0]?(R[0].innerText||'').trim():'';
        const heads=[...document.querySelectorAll('h1,h2,h3,h4,strong,b,div,p')].filter(e=>{
          const t=(e.innerText||'').trim(); const r=e.getBoundingClientRect();
          return t.length>6 && t.length<170 && (r.y+r.height/2)<rowY-15 && r.top>50
            && !/select your preferred|saves and advances|\\bhelp\\b|log out|skip to main/i.test(t)
            && ![...e.querySelectorAll('*')].some(c=>(c.innerText||'').trim()===t); });
        heads.sort((a,b)=>b.getBoundingClientRect().top-a.getBoundingClientRect().top);
        stem=heads[0]?(heads[0].innerText||'').trim():'';
      }
      return {is_pers:true, stem, left, right, n:digits.length};
    }"""

    # ---- NOA Exclusion Test = a TIMED figural odd-one-out cognitive test ("Select a figure that doesn't
    # fit", "N / 30", a countdown). The 5 answer choices are IMAGE cards rendered as buttons
    # ('Option K of 5'); a wrong pick still advances (SCORED, not gated) so we can COMPLETE the module.
    # Correct figural answers need a vision model (the local model is text-only) → we bank the screenshot
    # (needs_vision) and click a rotating placeholder to progress. A tutorial page ("Begin Assessment" /
    # "Repeat Tutorial") precedes it — the generic advance() clicks "Begin".
    _READ_NOA_JS = """() => {
      const opts=[...document.querySelectorAll('button,[role=button]')].filter(b=>{
        const t=(b.innerText||'').trim(); const a=(b.getAttribute('aria-label')||'').trim();
        return /^option\\s*\\d+$/i.test(t) || /option\\s*\\d+\\s*of\\s*\\d+/i.test(a); });
      if(opts.length<2) return {is_noa:false, n:opts.length};
      const body=(document.body.innerText||'');
      let q=''; const m=body.match(/select[^\\n]*?(fit|rule|figure|belong|different|odd)[^\\n]*/i);
      if(m) q=m[0].trim().slice(0,140);
      // the "N / M" progress makes every figure a DISTINCT item (same prompt text otherwise) so core's
      // advance-detector (keyed on question text) can tell figure N from N+1.
      const pm=body.match(/(\\d+)\\s*\\/\\s*(\\d+)/); const prog=pm? (pm[1]+'/'+pm[2]) : '';
      const timed=/time remaining/i.test(body) || !!pm;
      return {is_noa:true, n:opts.length, q, prog, timed};
    }"""

    # Job Knowledge / aptitude MCQ: a question + N radio options (letters A..H). Count the options + pull
    # the prompt text + the N/M progress (uniqueness for core's advance check).
    _READ_JK_JS = """() => {
      const radios=[...document.querySelectorAll('input[type=radio]')]
        .filter(r=>{const rr=r.getBoundingClientRect(); return true;});
      const letters=[...document.querySelectorAll('button,[role=button],label')]
        .filter(b=>/^[A-H]$/.test((b.innerText||'').trim()));
      const n = radios.length || letters.length;
      if(n<2) return {is_jk:false, n};
      const body=document.body.innerText||'';
      const lines=body.split('\\n').map(s=>s.trim())
        .filter(s=>s.length>12 && !/^[A-H]$/.test(s)
          && !/skip to main|^help$|log out|^continue$|time remaining|question \\d+ of/i.test(s));
      const q=lines.slice(0,3).join(' ').slice(0,200);
      const pm=body.match(/(\\d+)\\s*\\/\\s*(\\d+)/) || body.match(/question\\s+(\\d+)\\s+of\\s+(\\d+)/i);
      const prog=pm? (pm[1]+'/'+pm[2]) : '';
      return {is_jk:true, n, q, prog};
    }"""

    async def _click_jk_option(self, page, k: int) -> bool:
        """REAL click the k-th (0-based) Job-Knowledge option — the A..H letter button, else the k-th radio."""
        letter = "ABCDEFGH"[k] if 0 <= k < 8 else None
        if letter:
            try:
                loc = page.get_by_role("button", name=re.compile(rf"^{letter}$"))
                if await loc.count():
                    await loc.first.click(timeout=3000)
                    return True
            except Exception:
                pass
        try:
            radios = page.locator('input[type=radio]')
            if await radios.count() > k:
                await radios.nth(k).click(timeout=3000, force=True)
                return True
        except Exception:
            pass
        return False

    # Live Chat Support Simulation: a customer chats and you pick the BEST canned reply from options
    # labelled "Response 1..N" (like SJT, but a chat roleplay), then Next. Timed. Solve which response is
    # best via the OpenAI vision model on the screenshot; the customer text + first response give a per-turn
    # unique question so core's advance check works.
    _READ_CHAT_JS = """() => {
      const btns=[...document.querySelectorAll('button,[role=button]')];
      const resp=btns.filter(b=>/^response\\s*\\d+/i.test((b.innerText||'').trim()));
      if(resp.length<2) return {is_chat:false, n:resp.length};
      const rtext=resp.map(b=>(b.innerText||'').trim().slice(0,40));
      const lines=(document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
      const cust=lines.filter(t=>/(help|hello|please|can you|could you|\\?|order|return|refund|problem|issue|need|want|broken|wrong|cancel)/i.test(t)
        && !/^response/i.test(t) && !/skip to main|log out|^help$|view responses|time remaining|open the dialog/i.test(t)
        && t.length<160).slice(0,3);
      return {is_chat:true, n:resp.length, q:(cust.join(' ')+' || '+(rtext[0]||'')).slice(0,220)};
    }"""

    async def _click_chat_response(self, page, k: int) -> bool:
        """REAL click the k-th (0-based) canned chat reply ('Response k+1 …'). The reply may be a button,
        role=button, or a styled div — match by leading text across tags + click the clickable ancestor.
        A failed selection strands the module (the final Next stays disabled with no reply picked → the
        session times out), so try several REAL Playwright strategies (trusted events drive React), and if
        all miss, log the response-like elements actually present so the next chat hit is diagnosable."""
        rx = re.compile(rf"^\s*Response\s*{k + 1}\b", re.I)
        # 1) REAL Playwright clicks (trusted events → React registers the selection + enables Next).
        #    Try, in order: the accessible-name button, any tag filtered by leading text, the text node.
        for loc in (page.get_by_role("button", name=rx),
                    page.locator("button, [role=button], div, li, a, label").filter(has_text=rx),
                    page.get_by_text(rx)):
            try:
                if not await loc.count():
                    continue
                el = loc.first
                try:
                    await el.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                await el.click(timeout=3000)
                return True
            except Exception:
                continue
        # 2) JS fallback: click the clickable ancestor of the shortest matching element.
        try:
            if bool(await page.evaluate(
                "(k) => {"
                " const re=new RegExp('^\\\\s*response\\\\s*'+k+'\\\\b','i');"
                " const els=[...document.querySelectorAll('button,[role=button],div,li,label,a,span')]"
                "   .filter(e=>re.test((e.innerText||'').trim()));"
                " if(!els.length) return false;"
                " els.sort((a,b)=>((a.innerText||'').length)-((b.innerText||'').length));"
                " let el=els[0], n=el;"
                " for(let i=0;i<4&&n;i++){ const cs=getComputedStyle(n);"
                "   if(n.tagName==='BUTTON'||n.getAttribute('role')==='button'||n.onclick||cs.cursor==='pointer') break;"
                "   n=n.parentElement; }"
                " (n||el).click(); return true; }", k + 1)):
                return True
        except Exception:
            pass
        # 3) diagnostics — dump the response-like elements present so a persistent miss is debuggable.
        try:
            found = await page.evaluate(
                "() => [...document.querySelectorAll('button,[role=button],div,li,a,span')]"
                " .map(e=>({t:e.tagName,role:e.getAttribute('role'),al:e.getAttribute('aria-label'),"
                "          x:(e.innerText||'').trim().slice(0,44)}))"
                " .filter(o=>/response\\s*\\d/i.test(o.x)||/response\\s*\\d/i.test(o.al||'')).slice(0,8)")
            logger.info("[harver] CHAT click MISS k=%d response-like els=%s", k + 1, found)
        except Exception:
            pass
        return False

    # ---- Live-Chat driver (the WHOLE real-time sim, driven inside one answer_mcq call) ----
    # Full state probe of the chat module: the pickable "Response 1..N" options, the countdown, whether a
    # "Help another customer" switch is offered, and whether the chat DOM is present at ALL — so the driver
    # can tell "the customer is typing" (wait) from "the module ended" (advance to the next module).
    _CHAT_STATE_JS = r"""() => {
      const T = e => (e.innerText||e.textContent||'').replace(/\s+/g,' ').trim();
      const body = document.body.innerText || '';
      const rEls = [...document.querySelectorAll('button,[role=button],div,li,label,a')]
        .filter(e => /^\s*Response\s*\d+\b/i.test(T(e)));
      const byNum = {};                                   // keep the LEAF element per "Response N"
      for (const e of rEls) {
        const m = T(e).match(/^\s*Response\s*(\d+)/i); if (!m) continue;
        const k = m[1];
        if (!byNum[k] || T(e).length < T(byNum[k]).length) byNum[k] = e;
      }
      const nums = Object.keys(byNum).map(Number).sort((a,b)=>a-b);
      const responses = nums.map(n => T(byNum[n]).replace(/^\s*Response\s*\d+\s*/i,'').trim());
      const tm = body.match(/(\d+)\s*:\s*(\d{2})/);        // "MM:SS Time remaining"
      const helpA = [...document.querySelectorAll('a,button,[role=button]')]
        .find(e => /help another customer/i.test(T(e)));
      // A chat-SPECIFIC signal that persists between turns (while the customer 'types', no Response modal
      // shows) AND is absent from the next module (the Internet Speed Test), so it cleanly bounds the sim.
      // "Time remaining" alone is NOT chat-specific (cognitive/typing/speed-test modules are timed too).
      const chatUi = !!helpA
        || /using our live chat|live agent takeover|wait for .*response|customer id|help another customer/i.test(body);
      // A GATE modal precedes the real timed sim: a "Practice step done → Begin Assessment" tutorial gate
      // (its "Repeat Tutorial" sibling must NOT be clicked), a "Start the assessment" intro, etc. It sits
      // OVER the (frozen) practice Response boxes, so it MUST be dismissed before answering — else a JS
      // click lands on a covered Response and the sim never starts (the 12:00 timer stays frozen).
      const gateBtn = [...document.querySelectorAll('button,[role=button],a')].some(e =>
        /^(begin assessment|start assessment|begin the assessment|start the assessment|start chat)$/i.test(T(e)));
      const gateTxt = /practice step done|you can now begin the assessment|ready to begin the assessment|repeat the tutorial/i.test(body);
      return {
        n_responses: nums.length,
        responses,
        has_chat_ui: chatUi,
        has_gate: gateBtn || gateTxt,
        has_time_remaining: /time remaining/i.test(body),
        timer_secs: tm ? (parseInt(tm[1],10)*60 + parseInt(tm[2],10)) : null,
        has_help_another: !!helpA,
        typing: /typing\s*(?:\.\.\.|…)|wait for .*response/i.test(body),
        body_len: body.length,
      };
    }"""

    @staticmethod
    def _chat_is_live(state: dict) -> bool:
        """The chat-specific DOM is still present (a Response modal, the customer switcher, or a persistent
        chat-UI marker). When it is gone the module has transitioned to the next step (Internet Speed Test
        / completion). Keyed on CHAT-specific signals — NOT the bare countdown, which other timed modules
        (and possibly the speed test) also show, and which would otherwise trap the driver past the sim."""
        return bool(state.get("n_responses", 0) or state.get("has_chat_ui")
                    or state.get("has_help_another"))

    # Helpful/professional vs unhelpful customer-service phrasing — the FAST heuristic used when neither a
    # banked answer key nor the vision solver picks a reply (the sim only needs to COMPLETE, so a plausible
    # CS reply is enough).
    _CHAT_POS = ("apolog", "sorry", "i'll", "i will", "let me", "happy to", "of course", "assist",
                 "help", "resolve", "refund", "replace", "solution", "right away", "certainly",
                 "absolutely", "understand", "i can", "provide", "arrange", "process your", "send you",
                 "take care", "glad to", "look into", "check", "confirm", "of course", "thank you")
    _CHAT_NEG = ("not sure", "web search", "other resellers", "unfortunately", "can't", "cannot",
                 "no idea", "i don't know", "that's not my", "not my problem", "figure it out",
                 "nothing i can do", "you should have", "too bad", "not possible", "deal with it")

    def _heuristic_chat_pick(self, responses: list[str]) -> int | None:
        if not responses:
            return None
        best, best_s = 0, None
        for i, r in enumerate(responses):
            t = (r or "").lower()
            s = sum(w in t for w in self._CHAT_POS) - 2 * sum(w in t for w in self._CHAT_NEG)
            if best_s is None or s > best_s:
                best, best_s = i, s
        return best

    async def _pick_chat_response(self, page, q: str, responses: list[str], n: int) -> tuple[int, str]:
        """Choose the reply for this turn: (1) REPLAY a pre-solved bank answer key (the standard scripted
        customers are pre-solved), (2) LIVE vision solve, (3) the CS heuristic, (4) a rotating placeholder."""
        try:
            from backend.tools.assessment_harvester import bank as _bank
            msig = _bank.media_sig(["chat"] * max(1, len(responses) or n))
            opt_txt = [f"Response {i + 1}" for i in range(len(responses) or n)]
            ak = _bank.answer_for(self.platform, q, opt_txt, msig)
        except Exception:
            ak = None
        if ak:
            m = re.match(r"\s*response\s*(\d+)", (ak.get("text") or "").strip(), re.I)
            if m and 0 <= int(m.group(1)) - 1 < n:
                return int(m.group(1)) - 1, "answer_key"
            if isinstance(ak.get("index"), int) and 0 <= ak["index"] < n:
                return ak["index"], "answer_key"
        try:
            pick = await self._vision_pick(
                page, n, "You are a customer-service agent in a live chat. Choose the BEST reply to send "
                         "to the customer.", odd_one_out=False)
        except Exception:
            pick = None
        if pick is not None and 0 <= pick < n:
            return pick, "VISION"
        hp = self._heuristic_chat_pick(responses)
        if hp is not None and 0 <= hp < n:
            return hp, "heuristic"
        self._chat_i = getattr(self, "_chat_i", 0) + 1
        return self._chat_i % max(1, n), "placeholder"

    async def _dismiss_chat_gate(self, page) -> bool:
        """Click the chat intro/practice/tutorial GATE that blocks the real timed sim ('Practice step done →
        Begin Assessment', a 'Start the assessment' intro, a between-step 'Continue'/'Next section'). NEVER
        'Repeat Tutorial'/'Cancel'/'Decline'. The old per-turn handler got past this only by accident (its
        post-answer `_forward` matched 'Begin'); the driver dismisses it explicitly, BEFORE answering."""
        for rx in ("Begin Assessment", "Start Assessment", "Begin the Assessment", "Start the Assessment",
                   "Start Chat", "Get Started", "Begin", "Start", "Continue", "Proceed", "Next section"):
            if await self._click(page, rx, timeout=1200):
                return True
        try:
            return bool(await page.evaluate(
                "() => { const ok=/^(begin assessment|start assessment|begin the assessment|start the "
                "assessment|start chat|get started|begin|start|continue|proceed|next section)$/i;"
                " const b=[...document.querySelectorAll('button,[role=button],a')]"
                "  .find(e=>{const t=(e.innerText||'').trim();"
                "    return ok.test(t) && !/repeat|tutorial|cancel|decline|log ?out|back/i.test(t);});"
                " if(!b) return false; b.click(); return true; }"))
        except Exception:
            return False

    async def _click_help_another(self, page) -> bool:
        """Switch to the next waiting customer ('Help another customer' link)."""
        rx = re.compile("help another customer", re.I)
        for loc in (page.get_by_role("link", name=rx), page.get_by_role("button", name=rx),
                    page.get_by_text(rx)):
            try:
                if await loc.count():
                    await loc.first.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    async def _send_chat(self, page) -> bool:
        """After picking a Response, some variants need an explicit Send/Next; most auto-send on click."""
        for rx in ("Send", "Next", "Continue", "Submit"):
            if await self._click(page, rx, timeout=1200):
                return True
        return False

    async def _bank_chat_turn(self, page, q: str, responses: list[str], idx) -> None:
        """Best-effort capture of this chat turn (a screenshot + the options) so NEW customer scenarios
        keep landing in the bank. Guarded — a banking failure never breaks the walk."""
        try:
            from backend.tools.assessment_harvester import bank as _bank, media as _media
            opt_txt = [f"Response {i + 1}" for i in range(len(responses))]
            shot = await _media.capture(page, self.platform, q, opt_txt, page.url)
            opts = [{"text": t, "image": "chat", "audio": None} for t in opt_txt]
            chosen = {"text": (opt_txt[idx] if isinstance(idx, int) and 0 <= idx < len(opt_txt) else None),
                      "index": idx, "value": None, "source": "chat_driver"}
            _bank.record(platform=self.platform, item_type="unknown", question=q, options=opts,
                         media={"image": shot, "audio": None, "prompt_text": None},
                         chosen_answer=chosen, source={"mailbox": self.mailbox, "invite_url": None},
                         kind_meta={"is_scored": True, "is_ability": False},
                         msig=_bank.media_sig(["chat"] * max(1, len(opt_txt))))
        except Exception:
            pass

    async def _drive_chat(self, page) -> bool:
        """Drive the WHOLE Live-Chat Support Simulation to its end inside one call, then return True so
        core polls + advances to the next module (Internet Speed Test → completion). Answer each Response
        prompt (bank replay → vision → heuristic), tolerate the between-turn 'typing' gaps, switch to a
        waiting customer when the current chat idles, and EXIT once the chat DOM is gone (module ended) or
        the module's own countdown has expired. NEVER marks the assessment done — that stays with is_done,
        so a stuck chat can't be reported «пройдено»."""
        import time
        # Cover a WHOLE pass in one call: the practice/tutorial (~60s) + the real sim's own ~12-min (720s)
        # countdown + slack. A shorter budget still self-heals (core re-enters _drive_chat when the page
        # advanced), but 900s finishes the common case in a single pass.
        try:
            budget = float(os.getenv("HARVER_CHAT_BUDGET", "900"))
        except (TypeError, ValueError):
            budget = 900.0
        t0 = last_answer = time.monotonic()
        turns = idle = switches = 0
        logger.info("[harver] CHAT driver: start (budget=%ds)", int(budget))
        while time.monotonic() - t0 < budget:
            try:
                st = await page.evaluate(self._CHAT_STATE_JS)
            except Exception as e:
                st = {}
                logger.info("[harver] CHAT state probe err: %s", str(e)[:60])
            if not self._chat_is_live(st):
                # chat-specific DOM gone — likely the module transitioned. Re-confirm after a short settle
                # so a transient blank/re-render between turns isn't mistaken for the end.
                await page.wait_for_timeout(2000)
                try:
                    st = await page.evaluate(self._CHAT_STATE_JS)
                except Exception:
                    st = {}
                if not self._chat_is_live(st):
                    logger.info("[harver] CHAT driver: chat DOM gone → module ended (%d turns, %d switches)",
                                turns, switches)
                    await self._dump_controls(page, "chat_end", force_shot=True)
                    return True
            # A GATE modal (Practice step done → Begin Assessment / a tutorial-Start / a between-step
            # Continue) sits OVER the frozen practice responses — dismiss it BEFORE answering, else every
            # click lands on a covered Response and the real sim never starts (12:00 timer frozen).
            if st.get("has_gate"):
                if await self._dismiss_chat_gate(page):
                    idle = 0
                    last_answer = time.monotonic()
                    logger.info("[harver] CHAT driver: dismissed intro/practice gate (timer=%s)",
                                st.get("timer_secs"))
                    await page.wait_for_timeout(1800)
                    continue
                logger.info("[harver] CHAT driver: gate present but no CTA clicked — dumping")
                await self._dump_controls(page, "chat_gate_stuck", force_shot=True)
            n = st.get("n_responses", 0)
            if n >= 1:
                try:
                    probe = await page.evaluate(self._READ_CHAT_JS)
                except Exception:
                    probe = {}
                responses = st.get("responses") or []
                q = (probe.get("q") or "").strip() or ("chat: " + " | ".join(responses[:1]))[:220]
                idx, src = await self._pick_chat_response(page, q, responses, n)
                await self._bank_chat_turn(page, q, responses, idx)
                clicked = await self._click_chat_response(page, idx)
                await page.wait_for_timeout(500)
                sent = await self._send_chat(page)
                turns += 1
                idle = 0
                last_answer = time.monotonic()
                logger.info("[harver] CHAT turn %d: %s pick=%d/%d clicked=%s sent=%s timer=%s q=%r",
                            turns, src, idx + 1, n, clicked, sent, st.get("timer_secs"), q[:50])
                # FROZEN-CHAT guard: if the SAME response OPTIONS + SAME countdown recur for many turns the
                # sim is not advancing (a gate/overlay is eating the click, or the chat is wedged). Key on
                # the RESPONSE texts, NOT the question — a live chat (even a multi-turn practice, where the
                # reader keeps returning the transcript's first line as `q`) changes its reply options each
                # turn, while a wedged/covered modal keeps the exact same options. Don't burn the whole
                # budget clicking a covered Response — try a forward/gate CTA, then bail to core.
                sig = (tuple((r or "")[:40] for r in responses), st.get("timer_secs"))
                self._chat_stall = (getattr(self, "_chat_stall", 0) + 1
                                    if sig == getattr(self, "_chat_prev_sig", None) else 0)
                self._chat_prev_sig = sig
                if self._chat_stall >= 8:
                    logger.info("[harver] CHAT driver: FROZEN (same prompt+timer ×%d) — nudging then bailing",
                                self._chat_stall)
                    await self._dump_controls(page, "chat_frozen", force_shot=True)
                    await self._dismiss_chat_gate(page) or await self._forward(page)
                    await page.wait_for_timeout(1500)
                    fresh = {}
                    try:
                        fresh = await page.evaluate(self._CHAT_STATE_JS)
                    except Exception:
                        pass
                    if (fresh.get("timer_secs") == st.get("timer_secs")
                            and fresh.get("n_responses") and not fresh.get("has_gate")):
                        return True     # genuinely wedged — exit so core's is_done/advance can react
                    self._chat_stall = 0
                await page.wait_for_timeout(1200)          # let the customer 'type' the next line
                continue
            # No pickable response right now: the customer is typing, the current chat is done, or the
            # module has ended. Be patient (don't let core's stale counter fire), then nudge forward.
            idle += 1
            if idle >= 2 and st.get("has_help_another") and await self._click_help_another(page):
                switches += 1
                idle = 0
                last_answer = time.monotonic()
                logger.info("[harver] CHAT driver: switched to another customer (#%d)", switches)
                await page.wait_for_timeout(1500)
                continue
            if idle >= 2 and await self._forward(page):    # a between-module transition CTA, if any
                logger.info("[harver] CHAT driver: clicked a forward CTA during idle")
                idle = 0
                last_answer = time.monotonic()
                await page.wait_for_timeout(1200)
                continue
            if time.monotonic() - last_answer > 120 and not st.get("has_time_remaining"):
                logger.info("[harver] CHAT driver: idle >120s + no countdown → exiting")
                await self._dump_controls(page, "chat_idle_exit", force_shot=True)
                return True
            await page.wait_for_timeout(1500)
        logger.info("[harver] CHAT driver: budget reached (%d turns, %d switches) → exiting to core",
                    turns, switches)
        await self._dump_controls(page, "chat_budget", force_shot=True)
        return True

    async def read_item(self, page) -> dict:
        item = await super().read_item(page)
        try:
            title = await page.title()
        except Exception:
            title = ""
        tl = (title or "").lower()
        # SJT rate-each detection (must run BEFORE the generic video/mic routing — the scenario video would
        # otherwise route this to the video-answer branch instead of the MCQ branch).
        try:
            sjt = await page.evaluate(self._READ_SJT_JS)
        except Exception as e:
            sjt = {"is_sjt": False, "err": str(e)[:80]}
        if sjt.get("is_sjt") or sjt.get("nb"):
            logger.info("[harver] SJT probe: is_sjt=%s nb=%s nw=%s n=%s", sjt.get("is_sjt"),
                        sjt.get("nb"), sjt.get("nw"), sjt.get("n"))
        if sjt.get("is_sjt") and sjt.get("responses"):
            item = dict(item)
            item["question"] = sjt.get("scenario") or item.get("question", "") or "Situational judgement: rate best & worst"
            item["options"] = [{"text": r, "image": None} for r in sjt["responses"] if r]
            item["has_video"] = item["has_mic"] = item["has_textarea"] = item["has_audio"] = False
            item["qimgs"] = []
            item["_harver_sjt"] = True
            item["_no_llm_solve"] = True   # answer_mcq owns best/worst — skip core's wasted solve_one
        elif not item.get("options") and "personality" in tl:
            # Personality Questionnaire: a bipolar 6-point forced-choice (stem + left pole [=1] + right
            # pole [=6] + 6 circles; "answer saves and advances automatically"). GATED on the page title so
            # a section-end "Assessment completed" transition (which still has the last item's circles in
            # the DOM) or the NEXT module isn't misread as a personality item.
            try:
                pers = await page.evaluate(self._READ_PERS_JS)
            except Exception as e:
                pers = {"is_pers": False, "err": str(e)[:80]}
            if pers.get("is_pers") or pers.get("isPers"):
                logger.info("[harver] PERS probe: is_pers=%s n=%s stem=%r L=%r R=%r", pers.get("is_pers"),
                            pers.get("n") or pers.get("ns"), (pers.get("stem") or "")[:40],
                            (pers.get("left") or "")[:24], (pers.get("right") or "")[:24])
                if not pers.get("is_pers") and pers.get("dbg") and not getattr(self, "_pers_dbg_done", False):
                    self._pers_dbg_done = True
                    logger.info("[harver] PERS DBG cells: %s", pers.get("dbg"))
            if pers.get("is_pers"):
                item = dict(item)
                stem = pers.get("stem") or "Personality"
                left, right = pers.get("left") or "", pers.get("right") or ""
                item["question"] = f"{stem}  [1={left}] … [6={right}]".strip()
                item["options"] = [{"text": left or "left", "image": None},
                                   {"text": right or "right", "image": None}]
                item["has_video"] = item["has_mic"] = item["has_textarea"] = item["has_audio"] = False
                item["qimgs"] = []
                item["_harver_pers"] = True
                item["_pers_n"] = pers.get("n") or 6
                item["_no_llm_solve"] = True   # answered by heuristic in answer_mcq — skip core's solve_one
        # NOA Exclusion (figural odd-one-out cognitive), gated on the title.
        if (not item.get("_harver_sjt") and not item.get("_harver_pers")
                and ("noa" in tl or "exclusion" in tl)):
            try:
                noa = await page.evaluate(self._READ_NOA_JS)
            except Exception:
                noa = {"is_noa": False}
            if noa.get("is_noa"):
                _pg = noa.get("prog") or ""
                if _pg != getattr(self, "_noa_last_prog", None):   # log once per figure, not per signature poll
                    self._noa_last_prog = _pg
                    logger.info("[harver] NOA probe: %s n=%s q=%r timed=%s", _pg, noa.get("n"),
                                (noa.get("q") or "")[:50], noa.get("timed"))
                n = noa.get("n") or 5
                item = dict(item)
                prog = noa.get("prog") or ""
                base_q = noa.get("q") or "Select the figure that doesn't fit"
                item["question"] = (base_q + (f"  [{prog}]" if prog else "")).strip()
                item["progress"] = prog          # makes each figure a DISTINCT item for core's advance check
                # option image='figure' → has_img (core skips the text solve) + need_shot (banks the figure
                # image for offline vision solving); answer_mcq clicks a card + Continue.
                item["options"] = [{"text": f"Option {i + 1}", "image": "figure"} for i in range(n)]
                item["has_video"] = item["has_mic"] = item["has_textarea"] = item["has_audio"] = False
                item["_harver_noa"] = True
                item["_noa_n"] = n
                item["_no_llm_solve"] = True
        # Job Knowledge Test — a text/reasoning MCQ with A..H radio options (has a CORRECT answer → solved
        # live by the OpenAI vision model on the screenshot). Gated on title.
        if (not item.get("_harver_sjt") and not item.get("_harver_pers") and not item.get("_harver_noa")
                and ("knowledge" in tl or "aptitude" in tl or "reasoning" in tl or "numerical" in tl
                     or "verbal" in tl)):
            try:
                jk = await page.evaluate(self._READ_JK_JS)
            except Exception:
                jk = {"is_jk": False}
            if jk.get("is_jk"):
                _pg = jk.get("prog") or ""
                if _pg != getattr(self, "_jk_last_prog", None):
                    self._jk_last_prog = _pg
                    logger.info("[harver] JK probe: %s n=%s q=%r", _pg, jk.get("n"), (jk.get("q") or "")[:60])
                n = jk.get("n") or 4
                item = dict(item)
                item["question"] = ((jk.get("q") or "Job knowledge question")
                                    + (f"  [{_pg}]" if _pg else "")).strip()
                item["progress"] = _pg
                # image marker → has_img (core skips the weak local solve) + need_shot (banks the Q image)
                item["options"] = [{"text": f"Option {i + 1}", "image": "q"} for i in range(n)]
                item["has_video"] = item["has_mic"] = item["has_textarea"] = item["has_audio"] = False
                item["_harver_jk"] = True
                item["_jk_n"] = n
                item["_no_llm_solve"] = True
        # Live Chat Support Simulation — pick the best canned reply (Response 1..N) to the customer.
        # TITLE-INDEPENDENT: some vacancies title the tab with the company name, not "chat", so a title
        # gate missed the module entirely (→ core treated it as a dead landing → stuck). The "Response N"
        # pick modal is chat-specific (SJT uses Best/Neutral/Worst, personality uses circles), so detecting
        # by that modal (≥2 "Response N") is safe. answer_mcq then drives the WHOLE real-time sim.
        if (not item.get("_harver_sjt") and not item.get("_harver_pers") and not item.get("_harver_noa")
                and not item.get("_harver_jk")):
            try:
                chat = await page.evaluate(self._READ_CHAT_JS)
            except Exception:
                chat = {"is_chat": False}
            # Also treat the chat module as detected when its persistent chat-UI is present but the Response
            # modal hasn't rendered yet (the customer is still 'typing' the opening line) — else core would
            # stale-stuck at the chat ENTRY, before the driver engages.
            if not chat.get("is_chat"):
                try:
                    _cs = await page.evaluate(self._CHAT_STATE_JS)
                    if bool(_cs.get("has_chat_ui")):
                        chat = {"is_chat": True, "n": _cs.get("n_responses") or 3, "q": "chat sim"}
                except Exception:
                    pass
            if chat.get("is_chat"):
                n = chat.get("n") or 3
                q = chat.get("q") or "chat sim"
                if q[:40] != getattr(self, "_chat_last_q", None):
                    self._chat_last_q = q[:40]
                    logger.info("[harver] CHAT probe: n=%s q=%r", n, q[:70])
                item = dict(item)
                item["question"] = q
                item["options"] = [{"text": f"Response {i + 1}", "image": "chat"} for i in range(n)]
                item["has_video"] = item["has_mic"] = item["has_textarea"] = item["has_audio"] = False
                item["_harver_chat"] = True
                item["_chat_n"] = n
                item["_no_llm_solve"] = True
        # Dump each DISTINCT screen (SPA → same URL; dedup on title + question text) for capture.
        sig = (title + "::" + (item.get("question", "") or "")[:80]).strip().lower()
        seen = getattr(self, "_dumped_sigs", None)
        if seen is None:
            seen = self._dumped_sigs = set()
        if sig and sig not in seen:
            seen.add(sig)
            # Force a screenshot for a NEW/unhandled module type (so we can see its layout) even in fast
            # mode — SJT/personality are already understood and don't need per-item shots.
            force = any(k in tl for k in ("exclusion", "noa", "cognitive", "typing", "language",
                                          "reasoning", "numerical", "verbal", "skills", "aptitude",
                                          "interactive", "video", "logic", "knowledge", "job ",
                                          "chat", "simulation", "support"))
            await self._dump_controls(page, "read", force_shot=force)
        return item

    async def _solve_best_worst(self, question: str, responses: list[str]) -> tuple[int, int]:
        """Ask the local model (Sumrak) which response is BEST + which is WORST for a customer-service rep.
        Returns (best_idx, worst_idx); a robust fallback keeps the run moving on any parse/HTTP failure.
        The bank stores the question so a stronger offline pass can re-key it later (same as AMCAT)."""
        n = len(responses)
        # STRONG model FIRST: the SJT is scored, and a correct best/worst is the offer lever. The local
        # Sumrak model is weak at judgement; route through the OpenAI solver when available, fall back to
        # the local model, then a fixed pick — so the run always completes even with no solver.
        try:
            from backend.tools.assessment_harvester import openai_solver
            if openai_solver.available():
                import asyncio
                bw = await asyncio.to_thread(openai_solver.solve_best_worst, question, responses)
                if bw is not None:
                    return bw
        except Exception:
            pass
        numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(responses))
        prompt = (
            "You are a competent, reliable, customer-focused customer service representative taking a "
            "situational-judgement test. Read the scenario and the possible responses, then choose which "
            "ONE response is the BEST and which ONE is the WORST.\n\n"
            f"Scenario: {question or '(choose the best and worst response)'}\n\nResponses:\n{numbered}\n\n"
            "Answer with two numbers only — the BEST response number then the WORST response number, "
            "comma-separated, e.g. '2,3'.")
        try:
            import httpx
            from backend.config import settings
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(
                    f"{settings.llm_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
                    json={"model": settings.llm_model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.0, "max_tokens": 12, "stream": False})
                r.raise_for_status()
                txt = r.json()["choices"][0]["message"]["content"]
            nums = [int(x) - 1 for x in re.findall(r"\d+", txt or "")]
        except Exception:
            nums = []
        best = nums[0] if len(nums) >= 1 and 0 <= nums[0] < n else 0
        worst = nums[1] if len(nums) >= 2 and 0 <= nums[1] < n else n - 1
        if worst == best:
            worst = (best + 1) % n
        return best, worst

    # Undesirable / desirable customer-service traits — used by the FAST personality heuristic (no LLM:
    # a personality item has no objectively-correct answer, so we lean toward the professional pole by
    # keyword and cache the QUESTION in the bank for a stronger offline re-solve if ever wanted).
    _PERS_NEG = ("angr", "ignore", "avoid", "upset", "bother", "rude", "lazy", "careless", "quit",
                 "give up", "blame", "argue", "complain", "dislike", "refuse", "hate", "annoy",
                 "frustrat", "panic", "worry", "nervous", "aggress", "impatient", "yell", "snap",
                 "mislead", "dishonest", "forget", "distract", "avoidance")
    _PERS_POS = ("help", "calm", "patient", "listen", "understand", "find out", "solve", "friendly",
                 "reliable", "learn", "adapt", "enjoy", "care", "support", "empath", "clarif",
                 "polite", "honest", "organi", "focus", "positive", "cheer", "respect", "responsib",
                 "consist", "thorough", "prepared", "confident")

    async def _solve_personality(self, stem: str, left: str, right: str, n: int) -> int:
        """Bipolar 1..n item (1 = fully LEFT pole, n = fully RIGHT pole). FAST heuristic, no network: lean
        toward the pole with more desirable / fewer undesirable CS traits, with mild variation on ties so
        we don't straight-line a flat scale (some personality instruments flag careless straight-lining)."""
        def score(t: str) -> int:
            t = (t or "").lower()
            return sum(w in t for w in self._PERS_POS) - sum(w in t for w in self._PERS_NEG)
        sl, sr = score(left), score(right)
        self._pers_i = getattr(self, "_pers_i", 0) + 1
        if sr > sl:
            v = n if (sr - sl) >= 2 else n - 1
        elif sl > sr:
            v = 1 if (sl - sr) >= 2 else 2
        else:
            v = [n // 2 + 1, n // 2, n // 2 + 1, n - 1][self._pers_i % 4]  # gentle variation near center
        return min(max(v, 1), n)

    async def _vision_pick(self, page, n: int, question: str, odd_one_out: bool = True) -> int | None:
        """LIVE vision solve of the current on-screen MCQ (figural odd-one-out, or a knowledge/reasoning
        question) via the OpenAI vision model. Screenshots the page; returns a 0-based option index, or
        None when vision is unavailable/fails."""
        try:
            # CASCADE OpenRouter → Anthropic → OpenAI: try each available solver until one returns an
            # index. A solver that's unfunded (OpenRouter 402) self-disables via available()=False after
            # its first payment error, so the next call skips it and reaches a solver that still has credit.
            from backend.tools.assessment_harvester import (anthropic_solver, claude_cli_solver,
                                                            openai_solver, openrouter_solver)
            # claude_cli first when enabled: it runs on the local Claude subscription (no API credit), so
            # it's the free way to actually SOLVE (not placeholder) when the paid keys are out of balance.
            solvers = [s for s in (claude_cli_solver, openrouter_solver, anthropic_solver, openai_solver)
                       if s.available()]
            if not solvers:
                return None
            import asyncio
            import os as _os
            import tempfile
            tmp = _os.path.join(tempfile.gettempdir(),
                                f"harver_vis_{_os.getpid()}_{getattr(self, '_vis_i', 0)}.png")
            self._vis_i = getattr(self, "_vis_i", 0) + 1
            await page.screenshot(path=tmp)
            idx = None
            for solver in solvers:
                idx = await asyncio.to_thread(solver.solve_vision_mcq, tmp, n, question, odd_one_out)
                if idx is not None:
                    break
            try:
                _os.remove(tmp)
            except Exception:
                pass
            return idx
        except Exception as e:
            logger.info("[harver] vision pick err: %s", str(e)[:80])
            return None

    async def _click_noa_option(self, page, k: int) -> bool:
        """REAL Playwright click on the k-th (0-based) figure option — a JS .click() often doesn't drive the
        React selection. Tries the aria-label 'Option k+1 of N' then the role/text button."""
        for loc in (page.locator(f'[aria-label^="Option {k + 1} of"]'),
                    page.get_by_role("button", name=re.compile(rf"^Option\s*{k + 1}\b", re.I))):
            try:
                if await loc.count():
                    await loc.first.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    async def answer_mcq(self, page, item: dict, index: int) -> bool:
        """Harver SJT rate-each: solve best/worst; Personality: solve a 1..6 rating and click that circle
        (auto-advances); NOA figural: VISION-solve + real-click a choice + Continue. For any other MCQ,
        defer to the generic clicker."""
        if item.get("_harver_noa"):
            n = int(item.get("_noa_n") or 5)
            if isinstance(index, int) and 0 <= index < n:
                pick, src = index, "answer_key"   # REPLAY: core resolved this from the pre-solved bank
            else:
                pick = await self._vision_pick(page, n, item.get("question", ""))  # LIVE vision solve
                src = "VISION"
                if pick is None:           # vision unavailable/failed → rotating placeholder to keep moving
                    self._noa_i = getattr(self, "_noa_i", 0) + 1
                    pick = self._noa_i % n
                    src = "placeholder"
            clicked = await self._click_noa_option(page, pick)
            await page.wait_for_timeout(400)
            cont_ok = await self._click(page, "Continue")
            logger.info("[harver] NOA %s option %d/%d clicked=%s continue=%s", src, pick + 1, n,
                        clicked, cont_ok)
            await page.wait_for_timeout(600)
            return True
        if item.get("_harver_jk"):
            n = int(item.get("_jk_n") or 4)
            if isinstance(index, int) and 0 <= index < n:
                pick, src = index, "answer_key"   # REPLAY from the pre-solved bank
            else:
                pick = await self._vision_pick(page, n, item.get("question", ""), odd_one_out=False)
                src = "VISION"
                if pick is None:
                    self._jk_i = getattr(self, "_jk_i", 0) + 1
                    pick = self._jk_i % n
                    src = "placeholder"
            clicked = await self._click_jk_option(page, pick)
            await page.wait_for_timeout(400)
            cont_ok = await self._click(page, "Continue")
            logger.info("[harver] JK %s option %d/%d clicked=%s continue=%s", src, pick + 1, n,
                        clicked, cont_ok)
            await page.wait_for_timeout(600)
            return True
        if item.get("_harver_chat"):
            # The Live-Chat Support Simulation is a REAL-TIME, timer-bounded, multi-customer roleplay —
            # not a one-shot MCQ. Driving it one core-turn at a time was fragile: core's signature-based
            # advance detector (question+options) can't tell one chat turn from the next (the options are
            # always "Response 1..N" and a recurring customer prompt reproduces the same question text), so
            # it false-fires "no advance"; and the old 12-turn cap gave up by clicking Skip/Submit/Finish/
            # End/Done — none of which exist in this DOM (only Help / Log out / "Help another customer" /
            # ×) — so the whole session then stalled to max_steps. Drive the ENTIRE sim here instead.
            return await self._drive_chat(page)
        if item.get("_harver_pers"):
            n = int(item.get("_pers_n") or 6)
            opts = item.get("options") or []
            left = opts[0].get("text", "") if opts else ""
            right = opts[1].get("text", "") if len(opts) > 1 else ""
            stem = item.get("question", "")
            v = await self._solve_personality(stem, left, right, n)
            logger.info("[harver] PERS rate: v=%d/%d (L=%r R=%r)", v, n, left[:20], right[:20])
            # "answer saves and advances automatically" → the page can be mid-transition when we click.
            # RETRY the circle-click until it lands (a circle is found) so the race can't strand an item.
            _CLICK_JS = (
                "(v) => {"
                " const val=e=>{"
                "   const vv=((e.getAttribute&&e.getAttribute('value'))||'').trim();"
                "   if(/^[1-6]$/.test(vv)) return +vv;"
                "   const dv=((e.getAttribute&&e.getAttribute('data-value'))||'').trim();"
                "   if(/^[1-6]$/.test(dv)) return +dv;"
                "   const t=(e.innerText||e.textContent||'').trim(); if(/^[1-6]$/.test(t)) return +t;"
                "   const a=((e.getAttribute&&e.getAttribute('aria-label'))||'').trim();"
                "   const m=a.match(/^(-?[1-3])$/); if(m){const n=+m[1]; return n<0? n+4 : n+3;} return null; };"
                " const cells=[...document.querySelectorAll('button,[role=button],[role=radio],a,label,div,span')]"
                "   .map(e=>({e,v:val(e),r:e.getBoundingClientRect()}))"
                "   .filter(o=>o.v!==null && o.r.width>0 && o.r.width<90 && o.r.height<90"
                "     && ![...o.e.querySelectorAll('*')].some(c=>val(c)!==null));"
                " const t=cells.find(o=>o.v===v) || cells.find(o=>o.v===Math.min(6,Math.max(1,v)));"
                " if(!t) return false;"
                " let n=t.e; for(let i=0;i<4&&n;i++){ const cs=getComputedStyle(n);"
                "   if(n.tagName==='BUTTON'||n.getAttribute('role')==='button'||n.getAttribute('role')==='radio'"
                "     ||n.onclick||cs.cursor==='pointer') break; n=n.parentElement; } (n||t.e).click(); return true; }")
            clicked = False
            for _ in range(6):
                try:
                    clicked = await page.evaluate(_CLICK_JS, v)
                except Exception as e:
                    logger.info("[harver] PERS click err: %s", str(e)[:60]); clicked = False
                if clicked:
                    break
                await page.wait_for_timeout(500)   # wait for the circles to (re)render
            if clicked:
                await page.wait_for_timeout(400)   # auto-saves + advances (fast path)
                return True
            if not clicked:
                logger.info("[harver] PERS click: no circle found for v=%d (after retries)", v)
                try:
                    dbg = await page.evaluate(
                        "() => { const els=[...document.querySelectorAll('button,[role=button],[role=radio],a,label,input')]"
                        "  .filter(e=>{const r=e.getBoundingClientRect(); return r.width>0 && r.width<120 && r.height<120;})"
                        "  .slice(0,16).map(e=>{const r=e.getBoundingClientRect();"
                        "    return e.tagName+'/'+(e.getAttribute('role')||'')+'/'+(e.type||'')"
                        "      +' it='+JSON.stringify((e.innerText||'').trim().slice(0,8))"
                        "      +' al='+JSON.stringify((e.getAttribute('aria-label')||'').slice(0,8))"
                        "      +' v='+JSON.stringify((e.getAttribute('value')||'').slice(0,4))"
                        "      +' @'+Math.round(r.x)+'x'+Math.round(r.width);});"
                        "  return {title:document.title, body:(document.body.innerText||'').slice(0,160), els}; }")
                    logger.info("[harver] PERS click DBG title=%r body=%r", dbg.get("title"), dbg.get("body"))
                    logger.info("[harver] PERS click DBG els=%s", dbg.get("els"))
                except Exception as _e:
                    logger.info("[harver] PERS click DBG failed: %s", _e)
            await page.wait_for_timeout(1000)      # answer auto-saves + advances
            await self._forward(page)              # some variants still need a Continue
            return True
        if not item.get("_harver_sjt"):
            return await super().answer_mcq(page, item, index)
        responses = [o.get("text", "") for o in (item.get("options") or [])]
        best_i, worst_i = await self._solve_best_worst(item.get("question", ""), responses)
        logger.info("[harver] SJT rate: best=%d worst=%d of %d", best_i, worst_i, len(responses))
        try:
            await page.evaluate(
                "([bi,wi]) => {"
                " const leaves=(word)=>{ const all=[...document.querySelectorAll('*')].filter(e=>{"
                "   const t=(e.innerText||e.textContent||'').trim().toLowerCase();"
                "   const r=e.getBoundingClientRect(); return t===word && r.width>0 && r.height>0; });"
                "   return all.filter(e=>![...e.querySelectorAll('*')].some(c=>"
                "     (c.innerText||c.textContent||'').trim().toLowerCase()===word)); };"
                " const best=leaves('best'), worst=leaves('worst');"
                " const clk=el=>{ let n=el; for(let i=0;i<4&&n;i++){ const cs=getComputedStyle(n);"
                "   if(n.tagName==='BUTTON'||n.getAttribute('role')==='button'||n.getAttribute('role')==='radio'"
                "     ||n.onclick||cs.cursor==='pointer') break; n=n.parentElement; } (n||el).click(); };"
                " if(best[bi]) clk(best[bi]); if(worst[wi]) clk(worst[wi]); }", [best_i, worst_i])
        except Exception as e:
            logger.info("[harver] SJT click failed: %s", e)
            return False
        await page.wait_for_timeout(600)
        # Continue to the next scenario (enabled once one Best + one Worst are set)
        for _ in range(3):
            if await self._forward(page):
                await page.wait_for_timeout(1200)
                return True
            await page.wait_for_timeout(700)
        return True

    # Any gate / module title → NOT done (guards against a stray completion phrase in consent/intro text,
    # e.g. "Thank you for your interest" on the Consent page falsely matching).
    _NOT_DONE_TITLE = re.compile(
        r"consent|content page|test your camera|situational|personality|cognitive|typing|language|"
        r"judgment|judgement|hard ?skill", re.I)
    # A REAL Harver end-of-battery signal only — NOT a generic "thank you for your ...".
    _DONE_RE = re.compile(
        r"you have (now )?(successfully )?(completed|finished|submitted) (the|your|this) assessment|"
        r"assessment (is |has been )?(now )?complete(d)?\b|"
        r"thank you for completing (the|your|this) (assessment|evaluation|application|process)|"
        r"you'?re all done|there are no (more|further) (steps|assessments|tasks|sections)|"
        r"you have completed all|we have received your (results|application|answers)|"
        r"received your results so hang tight", re.I)

    async def handle_speaking(self, page, record_secs: float = 4.0, mic_say_wav=None) -> bool:
        """Harver read-aloud / SVAR speaking item. Accept the `mic_say_wav` kwarg core passes (the base
        Adapter.handle_speaking does NOT, which crashed the run), feed a gapless voice into the virtmic so
        the recorder captures audio, then STOP + advance to the next item."""
        self._start_mic_feed()
        try:
            await page.wait_for_timeout(8000)                 # let a few seconds record
            for sel in ('[aria-label*="stop" i]', 'button:has-text("Stop")', 'button:has-text("Submit")',
                        'button:has-text("Done")', 'button[class*="record" i]'):
                try:
                    b = page.locator(sel).first
                    if await b.count() and await b.is_visible():
                        await b.click(timeout=2000)
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(1500)
            for rx in ("Submit", "Next", "Continue", "Done", "Save", "Start"):
                if await self._click(page, rx, timeout=2000):
                    break
            await page.wait_for_timeout(1500)
            return True
        finally:
            self._stop_mic_feed()

    async def is_done(self, page) -> bool:
        # The DEFINITIVE Harver completion signal: the results page redirects to `...?completed=true`
        # (title "Thank you!", body "We have received your results…").
        try:
            if "completed=true" in (page.url or ""):
                if not getattr(self, "_done_logged", False):
                    self._done_logged = True
                    logger.info("[harver] is_done: completed=true URL — assessment submitted")
                    await self._dump_controls(page, "COMPLETE")
                return True
        except Exception:
            pass
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""
        if self._NOT_DONE_TITLE.search(title):
            return False
        try:
            body = (await page.inner_text("body", timeout=2500))
        except Exception:
            body = ""
        if self._DONE_RE.search(body):
            if not getattr(self, "_done_logged", False):
                self._done_logged = True
                logger.info("[harver] is_done matched on title=%r", title)
                await self._dump_controls(page, "COMPLETE")
            return True
        return False

    async def wall(self, page) -> str | None:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            return None
        if re.search(r"unable to detect a camera|enable your camera|camera (is )?required|"
                     r"we could(n't| not) access your camera", body):
            return "proctor_camera"
        if re.search(r"you have been (logged out|removed)|session (has )?expired|integrity", body):
            return "proctor"
        return None
