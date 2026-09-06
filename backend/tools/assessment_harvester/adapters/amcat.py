"""AMCAT / Aspiring Minds adapter (Teleperformance downstream) — an SHL TalentCentral / Aspiring
Minds Angular SPA at `amcatglobal.aspiringminds.com`.

Entry: the `?autoLoginVersion=3&token=<JWT>` autologin link from the "TP Assessment - Test Login
Details" email. Battery = numerical / verbal / logical / domain (cognitive MCQ), AMPI personality,
skill simulations, and sometimes SVAR speaking + typing.

Two constraints: a STALE token loads "completed or submitted / TC100" (open a FRESH one, within its
short window); and once open a per-phase countdown runs, so `enter()` clears the intro promptly but
with per-page transition detection (blitzing double-clicks a Continue and the SPA logs you out).

Intro chain (each cleared by `advance()`): Data Protection Notice (confirm checkbox + Continue) ->
Terms & Conditions (an `input.toggle-input` toggle + Continue) -> Copy Test ID (Next) -> System
Configuration Check. The diagnostic: a `.init-button` START runs OS/Browser/Speed/Latency/Mic/Speaker/
Webcam checks (VERIFIED they all pass on the datacenter box + fake mic/camera — ~18s), and when
`model.isProcessCompleted()` the footer SUBMIT enables as `#submit1` (the disabled placeholder is
`#submit3`) wired to `model.loadNextQuestion` — click THAT to reach the questions. Then per-section
intros (Next / I'm ready). `read_item()` reports a gate page as an empty landing item so the core
routes it to advance(); real questions pass through for the random-answer path.
"""
from __future__ import annotations

import logging
import re

from backend.tools.assessment_harvester.adapters.base import Adapter

logger = logging.getLogger("assessment_harvester")

_TERMINAL_RE = re.compile(
    r"completed or submitted|no further action|message code tc|assessment time ?out|"
    r"time allotted has expired|has expired|no longer available|link is invalid|"
    r"session (has )?expired|been successfully logged out|"
    r"error code mic\d*|logged out because your mic|you have been logged out", re.I)

_PROCTOR_WALL_RE = re.compile(
    r"unable to detect a camera|camera is mandatory|camera not detected|enable your camera|"
    r"we could not detect your (camera|webcam)", re.I)

_GATE_RE = re.compile(
    r"system configuration|system diagnostic tool|minimum requirement|terms and conditions|"
    r"data protection notice|copy test id.{0,40}future reference|content of this test is confidential|"
    r"i confirm i have read|please read the instructions|instructions? for (this|the) (test|section)", re.I)

# timer "MM:SS" (tolerates spaces around the colon, e.g. "03 : 41")
_TIMER_JS = r"() => { const m=(document.body.innerText||'').match(/\b([0-5]?\d)\s*:\s*([0-5]\d)\b/); return m ? (m[1]+':'+m[2]) : null; }"

# The enabled diagnostic SUBMIT (#submit1, or a visible non-disabled footer green button reading
# SUBMIT). Distinct from the disabled placeholder #submit3.
_DIAG_SUBMIT_DETECT_JS = r"""() => {
  const v=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2;};
  const dis=e=>/\bdisabled\b/.test(e.className||'')||e.getAttribute('aria-disabled')==='true';
  let s=document.querySelector('#submit1');
  if(!s||!v(s)||dis(s)) s=[...document.querySelectorAll('#submitBtn a,#submitBtn button,a.footerBtn.btn-green-bs,.footerBtn.btn-green-bs,a.btn-green-bs')]
     .find(e=>v(e)&&!dis(e)&&/submit/i.test((e.innerText||e.textContent||'')));
  return !!(s&&v(s)&&!dis(s));
}"""
_DIAG_SUBMIT_CLICK_JS = r"""() => {
  const v=e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2;};
  const dis=e=>/\bdisabled\b/.test(e.className||'')||e.getAttribute('aria-disabled')==='true';
  let s=document.querySelector('#submit1');
  if(!s||!v(s)||dis(s)) s=[...document.querySelectorAll('#submitBtn a,#submitBtn button,a.footerBtn.btn-green-bs,.footerBtn.btn-green-bs,a.btn-green-bs')]
     .find(e=>v(e)&&!dis(e)&&/submit/i.test((e.innerText||e.textContent||'')));
  if(s&&v(s)&&!dis(s)){ s.scrollIntoView({block:'center'}); s.click(); return true; }
  return false;
}"""
_ON_DIAG_JS = r"""() => !!document.querySelector('.init-button,[ng-click*="model.start"],#submitBtn,.element-row')"""

# One gate action per call: tick consent/T&C toggles, run diagnostic START, click the enabled diag
# SUBMIT (ONLY before the diagnostic is submitted — see the single-click discipline below), else
# click the primary forward control. Returns a short tag (or null).
#
# CRITICAL single-click discipline (`diagDone` arg): the diagnostic SUBMIT is `#submit1`, but that
# id is REUSED by later SVAR modal buttons (YES / TRY AGAIN / OK all render as `#submit1` too), and
# RE-clicking the diagnostic submit logs the SPA out ("You have successfully logged out" -> 0 banked).
# So once the diagnostic has been submitted once, this gate NEVER touches `#submit1` / any "submit"
# button again — SVAR primary CTAs are driven by `handle_speaking`, not here.
_GATE_ACTION_JS = r"""(diagDone) => {
  const vis = el => { if(!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const T = el => (el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
  const clickable = b => !/\bdisabled\b/.test(b.className||'') && b.getAttribute('aria-disabled')!=='true';
  let checked = 0;
  document.querySelectorAll('input[type=checkbox]').forEach(c => {
     const box = c.closest('label') || c;
     if (!c.checked && vis(box)) { try { c.click(); checked++; } catch(e){} } });
  const startEl = [...document.querySelectorAll('.init-button, [ng-click*="model.start"]')].find(vis);
  if (startEl) { startEl.click(); return 'diag_start'; }
  if (!diagDone) {
    let dsub = document.querySelector('#submit1');
    if(!dsub||!vis(dsub)||!clickable(dsub)) dsub=[...document.querySelectorAll('#submitBtn a,#submitBtn button,a.footerBtn.btn-green-bs,.footerBtn.btn-green-bs,a.btn-green-bs')].find(e=>vis(e)&&clickable(e)&&/submit/i.test(T(e)));
    if (dsub && vis(dsub) && clickable(dsub)) { dsub.scrollIntoView({block:'center'}); dsub.click(); return 'diag_submit'; }
  }
  const fwdRe = /^(continue|next|proceed|start test|start assessment|begin( test)?|i'?m ready|ready|ok|go|start now|start|save (and|&) (next|continue)|got it)$/i;
  const denyRe = /cancel|exit|\bback\b|help|skip|log ?out|previous|restart|full ?screen|sidemenu|re-?run|rerun|submit|try again/i;
  // After the diagnostic, NEVER click a bare #submit1 (a persisted/repurposed diagnostic-submit id
  // whose re-click logs out). SVAR primary CTAs are handled elsewhere.
  const forbidden = b => diagDone && (b.id==='submit1');
  const cand = [...document.querySelectorAll('.continue-button,.btn-green-bs,[class*="rightBtn"],a.footerBtn,button,[role=button],a,input[type=submit]')].filter(vis);
  let btn = cand.find(b => clickable(b) && !forbidden(b) && /continue-button|btn-green-bs/.test(b.className||'') && !denyRe.test(T(b)));
  if (!btn) btn = cand.find(b => { const t=T(b); return t && !denyRe.test(t) && fwdRe.test(t) && clickable(b) && !forbidden(b); });
  if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); return 'fwd:'+(T(btn)||btn.className).slice(0,24); }
  return checked ? ('checked'+checked) : null;
}"""

# ---- SVAR (Section A: Read and Speak) speaking-page state ---------------------------------------
# The prompt to speak lives in `.svar-las-question-block .mBoldFont` (NOT `#question`, which is the
# empty `<audio-wave-comp>` waveform). The primary green action is `.primary-cta-btn` whose inner
# span text CYCLES (OK -> YES -> record/SUBMIT ANSWER -> NEXT); when busy/recording it carries the
# `disabled` class. The secondary "TRY AGAIN" is `.secondary-cta-btn` (NEVER clicked — selecting
# `.primary-cta-btn` already excludes it). Nav = `#questionMobBtn` ("Q1 / 28") + `.currentQue`.
_SVAR_STATE_JS = r"""() => {
  const vis = el => { if(!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const T = el => (el ? (el.textContent||'') : '').replace(/\s+/g,' ').trim();
  const isSvar = !!document.querySelector('.svar-las, .svar-footer-row, .svarOuter, audio-wave-comp');
  let prompt='';
  const blk = [...document.querySelectorAll('.svar-las-question-block')].find(vis)
            || document.querySelector('.svar-las-question-block');
  if (blk){
     prompt = T(blk.querySelector('.mBoldFont'));
     if(!prompt){ const ps=[...blk.querySelectorAll('p')].filter(p=>!/svar-direction/.test(p.className||'')); if(ps.length) prompt=T(ps[ps.length-1]); }
  }
  if(!prompt){ const b=[...document.querySelectorAll('.mBoldFont')].filter(vis); if(b.length) prompt=T(b[0]); }
  const direction = T([...document.querySelectorAll('.svar-direction')].filter(vis)[0]);
  const section = T([...document.querySelectorAll('.svar-questionHeading')].filter(vis)[0]);
  const mob = document.querySelector('#questionMobBtn');
  const navTxt = mob ? T(mob) : T(document.querySelector('.currentQue'));
  // NEVER pick the re-record / retreat actions (TRY AGAIN re-records the same item -> infinite loop).
  const RE_SKIP = /try ?again|record ?again|re-?record|\bretry\b|\bcancel\b|\bback\b|\bexit\b|\bskip\b|\bhelp\b|listen again/i;
  const RE_FWD  = /submit answer|\bsubmit\b|\bnext\b|^ok$|\byes\b|continue|proceed|^done$|\bsave\b|start/i;
  const inDlg = e => e.closest('.ngdialog,[role=dialog],.info-body,.modal-popup,.modal');
  const allPrim = [...document.querySelectorAll('.primary-cta-btn')].filter(vis);
  const enabled = allPrim.filter(e => !/\bdisabled\b/.test(e.className||'') && e.getAttribute('aria-disabled')!=='true'
                                   && !RE_SKIP.test(T(e)));
  const disabledPrim = allPrim.filter(e => /\bdisabled\b/.test(e.className||'') || e.getAttribute('aria-disabled')==='true');
  // Prefer a FORWARD action (SUBMIT ANSWER / NEXT / OK / YES / CONTINUE), a dialog one first; then any
  // actionable (ng-click) one; then the first. Never the bare handler-less footer button when a real
  // action exists, and never TRY AGAIN.
  const cta = enabled.find(e => inDlg(e) && RE_FWD.test(T(e)))
           || enabled.find(e => RE_FWD.test(T(e)))
           || enabled.find(e => inDlg(e))
           || enabled.find(e => e.hasAttribute('ng-click') || e.hasAttribute('data-ng-click'))
           || enabled[0] || null;
  const modal = T([...document.querySelectorAll('.info-heading, .ngdialog-content .list-container, .info-text-section')].filter(vis)[0]);
  const heads = [...document.querySelectorAll('.questionHeading, h2')].filter(vis).map(T);
  // POST-record REVIEW state: "Play Recording" + a playback waveform, with NEXT disabled until you
  // play back your answer ("Click NEXT if you can hear your voice clearly").
  const review = !!([...document.querySelectorAll('.playRecording, .recordingPlayAgain')].find(vis))
              || heads.some(h => /play recording/i.test(h));
  // ACTIVE recording: "Speak Now" / "please speak" (NOT "Play Recording").
  const recording = !review && heads.some(h => /speak now|please speak/i.test(h));
  // "Warning! We are unable to hear you." modal (TRY AGAIN re-records, TRY LATER defers 2 min).
  const bodyTxt = document.body.innerText || '';
  const warn = /unable to hear you|problem occurred with your microphone|try again to start the recording/i.test(bodyTxt);
  return {isSvar, prompt, direction, section, navTxt,
          ctaText: cta ? T(cta) : null, hasCta: !!cta,
          disabledCta: disabledPrim.length ? T(disabledPrim[0]) : null,
          recording, review, warn, heads: heads.slice(0,4), modal};
}"""

# Click the single ENABLED primary CTA once. Returns its label (or null if none enabled).
_SVAR_CLICK_PRIMARY_JS = r"""() => {
  const vis = el => { if(!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const RE_SKIP = /try ?again|record ?again|re-?record|\bretry\b|\bcancel\b|\bback\b|\bexit\b|\bskip\b|\bhelp\b|listen again/i;
  const RE_FWD  = /submit answer|\bsubmit\b|\bnext\b|^ok$|\byes\b|continue|proceed|^done$|\bsave\b|start/i;
  const inDlg = e => e.closest('.ngdialog,[role=dialog],.info-body,.modal-popup,.modal');
  const enabled = [...document.querySelectorAll('.primary-cta-btn')].filter(e => vis(e)
       && !/\bdisabled\b/.test(e.className||'') && e.getAttribute('aria-disabled')!=='true'
       && !RE_SKIP.test(T(e)));
  // same forward-first / dialog-first preference as the state reader; never TRY AGAIN
  const el = enabled.find(e => inDlg(e) && RE_FWD.test(T(e)))
          || enabled.find(e => RE_FWD.test(T(e)))
          || enabled.find(e => inDlg(e))
          || enabled.find(e => e.hasAttribute('ng-click') || e.hasAttribute('data-ng-click'))
          || enabled[0];
  if(!el) return null;
  const label=T(el);
  el.scrollIntoView({block:'center'}); el.click();
  return label || 'click';
}"""

# Click a button by its visible text (TRY AGAIN / TRY LATER in the "unable to hear" warning modal).
_SVAR_CLICK_TEXT_JS = r"""(pat) => {
  const re = new RegExp(pat, 'i');
  const vis = el => { if(!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const els = [...document.querySelectorAll('.primary-cta-btn,.secondary-cta-btn,button,a,[role=button]')].filter(vis);
  const el = els.find(e => re.test(T(e)) && T(e).length < 24);
  if(!el) return null;
  const lbl = T(el); el.scrollIntoView({block:'center'}); el.click();
  return lbl || 'click';
}"""

# In the post-record REVIEW state, PLAY the recorded answer — this is what enables the (otherwise
# permanently disabled) NEXT button. The play control is `button.pure-btn` carrying a
# `.recordingPlayAgain` icon (or the "play" span); the `.replay-demo-btn` REPLAY link is the fallback.
_SVAR_PLAY_REVIEW_JS = r"""() => {
  const vis = el => { if(!el) return false; const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  let b = [...document.querySelectorAll('.playRecording button, .upperOptDivOnWave button, button.pure-btn')]
    .find(e => vis(e) && (e.querySelector('.recordingPlayAgain, .recordingPlayIcon') || /\bplay\b/i.test(e.textContent||'')));
  if(!b) b = [...document.querySelectorAll('[ng-click*="replay"], .replay-demo-btn')].find(vis);
  if(!b) return null;
  const lbl=(b.textContent||'').replace(/\s+/g,' ').trim();
  b.scrollIntoView({block:'center'}); b.click();
  return lbl || 'play';
}"""

# instructional prose that is NOT a speak-this-sentence prompt (so the Section-A intro isn't
# mis-banked as a speaking item)
_SVAR_INTRO_RE = re.compile(
    r"in this section|speak the sentence|the test will (automatically )?move|"
    r"read the sentence(s)? (aloud|out loud) (that|which|displayed)|"
    r"listen carefully$|you may (also )?click", re.I)


class AmcatAdapter(Adapter):
    platform = "amcat"

    def __init__(self):
        # Once the diagnostic SUBMIT (#submit1) has been clicked ONCE, never target #submit1 again
        # (its id is reused by SVAR modal buttons and re-clicking the diag submit logs the SPA out).
        self._diag_submitted = False

    async def _timer(self, page):
        try:
            return await page.evaluate(_TIMER_JS)
        except Exception:
            return None

    async def is_terminal(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            body = ""
        return bool(_TERMINAL_RE.search(body))

    async def svar_state(self, page) -> dict:
        try:
            return await page.evaluate(_SVAR_STATE_JS) or {}
        except Exception:
            return {}

    async def read_item(self, page) -> dict:
        it = await super().read_item(page)
        # A terminal / mic-logout page must NEVER be banked as a speaking item (it looks like an SVAR
        # page — svar chrome + a "prompt" that is really the error text). Let the core wall() catch it.
        if _TERMINAL_RE.search(it.get("body") or ""):
            it = dict(it)
            it["options"] = []
            it["has_mic"] = it["has_textarea"] = it["has_audio"] = it["has_video"] = False
            it["qimgs"] = []
            return it
        real_opts = [o for o in (it.get("options") or []) if (o.get("text") or "").strip()]
        # SVAR "Read and Speak": a real sentence prompt + svar chrome, no MCQ options -> speaking.
        # (The Section-A intro carries instructional prose, not a sentence -> excluded by _SVAR_INTRO_RE.)
        if not real_opts:
            sv = await self.svar_state(page)
            p = (sv.get("prompt") or "").strip()
            if sv.get("isSvar") and len(p) >= 6 and not _SVAR_INTRO_RE.search(p):
                it = dict(it)
                it["question"] = p
                it["options"] = []
                it["has_mic"] = True
                it["has_textarea"] = it["has_audio"] = it["has_video"] = False
                it["qimgs"] = []
                it["_svar"] = sv
                return it
        if _GATE_RE.search(it.get("body") or ""):
            it = dict(it)
            it["options"] = []
            it["has_mic"] = it["has_textarea"] = it["has_audio"] = it["has_video"] = False
            it["qimgs"] = []
        return it

    async def _diag_blitz(self, page) -> bool:
        """START clicked. Poll for the enabled diagnostic SUBMIT (#submit1); click it the moment it
        appears (checks pass in ~18s). Cap ~40s."""
        for k in range(52):
            await page.wait_for_timeout(750)
            try:
                ready = await page.evaluate(_DIAG_SUBMIT_DETECT_JS)
            except Exception:
                ready = False
            if ready:
                logger.info("[amcat] diag SUBMIT ready ~%ds timer=%s", int(k * 0.75), await self._timer(page))
                try:
                    await page.evaluate(_DIAG_SUBMIT_CLICK_JS)
                except Exception:
                    pass
                self._diag_submitted = True  # single-click discipline: never touch #submit1 again
                return True
            try:
                on = await page.evaluate(_ON_DIAG_JS)
            except Exception:
                on = True
            if not on:
                return True
            if k in (10, 26, 51):
                logger.info("[amcat] diag waiting ~%ds timer=%s", int(k * 0.75), await self._timer(page))
        return False

    async def advance(self, page) -> bool:
        try:
            act = await page.evaluate(_GATE_ACTION_JS, self._diag_submitted)
        except Exception:
            act = None
        if act == "diag_start":
            await self._diag_blitz(page)
            return True
        return bool(act)

    async def enter(self, page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2800)
        logger.info("[amcat] OPEN timer=%s terminal=%s", await self._timer(page), await self.is_terminal(page))
        prev = ""
        for i in range(40):
            if await self.is_terminal(page):
                logger.info("[amcat] TERMINAL step %d timer=%s", i, await self._timer(page))
                return
            item = await self.read_item(page)
            real = [o for o in (item.get("options") or []) if (o.get("text") or "").strip()]
            if len(real) >= 2 or item.get("has_mic") or item.get("has_textarea"):
                logger.info("[amcat] REACHED QUESTION step %d timer=%s q=%r opts=%d",
                            i, await self._timer(page), item.get("question", "")[:60], len(real))
                return
            try:
                act = await page.evaluate(_GATE_ACTION_JS, self._diag_submitted)
            except Exception:
                act = None
            if act == "diag_start":
                await self._diag_blitz(page)
            logger.info("[amcat] gate step %d act=%s diag_done=%s timer=%s",
                        i, act, self._diag_submitted, await self._timer(page))
            # per-page transition wait (avoid double-click logout): wait until the body changes, cap ~4s
            cur = ""
            try:
                cur = await page.inner_text("body", timeout=2000)
            except Exception:
                pass
            if cur[:120] == prev[:120]:
                for _ in range(14):
                    await page.wait_for_timeout(250)
                    try:
                        nb = await page.inner_text("body", timeout=2000)
                    except Exception:
                        nb = cur
                    if nb[:120] != cur[:120]:
                        break
            else:
                await page.wait_for_timeout(500)
            prev = cur

    async def handle_speaking(self, page, record_secs: float = 6.0) -> bool:
        """Drive ONE SVAR speaking item to the next. The primary CTA cycles OK -> YES (device-test
        "were you able to hear it?") -> records (fake mic feeds speech.wav; CTA goes `disabled`) ->
        SUBMIT ANSWER / NEXT. Click the ENABLED `.primary-cta-btn` ONE click per state, waiting for a
        REAL transition between clicks (never a rapid double-click -> logout); wait while the CTA is
        disabled (recording), watching for an auto-advance. Advance = the nav number ("Q1 / 28") or
        the prompt changes, or we leave the SVAR section. Returns True if it advanced, else False."""
        st = await self.svar_state(page)
        start_nav = st.get("navTxt")
        start_prompt = st.get("prompt")
        last_label = None
        same = 0
        played_nav = None
        warn_ticks = 0
        # PATIENT per-item walk (owner-verified: "just wait — NEXT/advance opens on its own"). Each item:
        # question audio plays -> RECORD phase (rec=True; the fake mic speaks speech.wav — do NOT click,
        # just wait so a real recording is captured; clicking SUBMIT with no recording is what triggers
        # the "unable to hear you" WARN) -> SUBMIT ANSWER / review(PLAY)->NEXT. Click a forward CTA ONCE
        # then wait; never hammer (rapid re-clicks caused the WARN loop). ~150 ticks (~7min) budget.
        for tick in range(150):
            cur = await self.svar_state(page)
            if not cur.get("isSvar") or await self.is_done(page):
                return True                      # left SVAR / finished
            nav = cur.get("navTxt")
            if (nav and nav != start_nav) or (cur.get("prompt") and start_prompt and cur.get("prompt") != start_prompt):
                logger.info("[amcat] svar advanced nav %s -> %s", start_nav, nav)
                return True
            if tick % 4 == 0:
                logger.info("[amcat] svar wait t=%d nav=%s cta=%r disCta=%r rec=%s review=%s warn=%s",
                            tick, nav, cur.get("ctaText"), cur.get("disabledCta"),
                            cur.get("recording"), cur.get("review"), cur.get("warn"))

            # ---- "unable to hear you" WARN: be PATIENT, do NOT hammer (rapid TRY AGAIN was the bug) ----
            # Wait calmly; TRY AGAIN only every ~15s so each re-record has time to capture the fake mic.
            # Never click TRY LATER (-> MIC200 logout). Give up only after a long patient window.
            if cur.get("warn"):
                warn_ticks += 1
                if warn_ticks > 40:              # ~120s of patience
                    logger.info("[amcat] svar WARN persists ~120s nav=%s", nav)
                    return False
                if warn_ticks % 5 == 0:          # a gentle retry every ~15s, not every tick
                    await page.evaluate(_SVAR_CLICK_TEXT_JS, r"try ?again")
                    logger.info("[amcat] svar WARN patient TRY AGAIN (%ds) nav=%s", warn_ticks * 3, nav)
                await page.wait_for_timeout(3000)
                continue
            warn_ticks = 0

            # ---- forward-first: click any ENABLED primary CTA (SUBMIT ANSWER / NEXT / OK / YES) once ----
            if cur.get("hasCta") and cur.get("ctaText"):
                label = cur.get("ctaText")
                same = same + 1 if label == last_label else 0
                if same >= 20:                   # same enabled CTA not progressing -> give up this item
                    logger.info("[amcat] svar stuck on CTA=%r nav=%s", label, nav)
                    await self._dump_stuck(page, "svar_cta")
                    return False
                clicked = await page.evaluate(_SVAR_CLICK_PRIMARY_JS)
                last_label = label
                logger.info("[amcat] svar click %r nav=%s prompt=%r", clicked, nav, (cur.get("prompt") or "")[:44])
                await page.wait_for_timeout(2800)
                continue

            # ---- REVIEW with no enabled CTA: PLAY the recording once to enable the disabled NEXT ----
            if cur.get("review") and played_nav != nav:
                pl = await page.evaluate(_SVAR_PLAY_REVIEW_JS)
                played_nav = nav
                last_label = None
                logger.info("[amcat] svar review PLAY=%r nav=%s", pl, nav)
                await page.wait_for_timeout(3500)
                continue

            # ---- else: question audio playing / recording / processing — just WAIT (patience) ----
            last_label = None
            await page.wait_for_timeout(2500)
        logger.info("[amcat] svar budget exhausted nav=%s", start_nav)
        await self._dump_stuck(page, "svar_budget")
        return False

    async def _dump_stuck(self, page, tag: str) -> None:
        """Best-effort DOM+screenshot of a stuck SVAR state, for offline tuning. Never raises."""
        import os
        d = os.environ.get("AMCAT_DUMP_DIR",
                           "/tmp/claude-1000/-home-projects-jobfinder/"
                           "583d6b11-c6db-484c-ad41-20627c61a55e/scratchpad/svar_dump")
        try:
            os.makedirs(d, exist_ok=True)
            await page.screenshot(path=os.path.join(d, f"{tag}.png"))
            html = await page.content()
            with open(os.path.join(d, f"{tag}.html"), "w", encoding="utf-8") as f:
                f.write(html[:600000])
            logger.info("[amcat] dumped stuck state -> %s/%s.{png,html}", d, tag)
        except Exception:
            pass

    async def wall(self, page) -> str | None:
        if await self.is_terminal(page):
            return "terminal_token"
        try:
            body = await page.inner_text("body", timeout=3000)
        except Exception:
            body = ""
        if _PROCTOR_WALL_RE.search(body):
            return "proctor_camera"
        return None
