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
import os
import re

from backend.tools.assessment_harvester.adapters.base import Adapter
from backend.tools.assessment_harvester import mic, assets

# Section D "Free Speech": speak on a topic for a timed window. SUBMIT must NOT be clicked while
# recording (an early click with an unfinished recording triggers the "unable to hear you" WARN loop);
# feed the mic continuously and let the speaking window run out, then submit.
_FREE_SPEECH_RE = re.compile(r"free speech|speak on the topic|your topic is|describe (a|an|your|the)\b", re.I)

# A long, continuous passage to feed the mic during a free-speech window so the recorder hears sustained
# speech (a short clip with replay gaps reads as "unable to hear you").
_FREE_SPEECH_TEXT = (
    "Last year I stayed at a seaside resort with my family for a week. "
    "The rooms were clean and comfortable, and the staff were friendly and helpful at all times. "
    "Every morning we had breakfast by the pool and then walked along the beach. "
    "In the afternoons we joined activities like kayaking and tennis, and in the evening we enjoyed dinner "
    "at the restaurant. The weather was warm and sunny, and the whole trip was relaxing and memorable. "
    "I would happily recommend that resort to anyone looking for a calm and pleasant holiday.")

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

# Section C "Listening Comprehension" (and any AMCAT MCQ) options render as `.option-lable`
# (note the source typo "lable") inside `.rander-options`, NOT as radios/`.question-answer-label`, so
# the generic reader misses them and the page (svar chrome, no detected options) was mis-routed to the
# speaking handler and stalled. Extract the options + the question + whether a listen clip is present.
_MCQ_OPTS_JS = r"""() => {
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const els = [...document.querySelectorAll('.option-lable')].filter(vis);
  const seen = new Set(); const opts = [];
  els.forEach(e => { const t=T(e); if(!t||seen.has(t)||t.length>400) return; seen.add(t);
     const im=e.querySelector('img'); opts.push({text:t, image: im ? (im.src||'') : null}); });
  let q='';
  const cand = [...document.querySelectorAll('.mBoldFont, .svar-las-question-block p, .question-text, .rander-question, .svar-direction')]
     .filter(vis).map(T).filter(x => x && !seen.has(x) && x.length>3);
  if (cand.length) q = cand.find(x => /\?\s*$/.test(x)) || cand[cand.length-1] || '';
  const hasAudio = !!document.querySelector('#audio-blob-player, audio[src*="SpeechAssessmentBank"], audio[src]');
  return {opts, q, hasAudio};
}"""

# Click the nth visible `.option-lable` (Section C MCQ). Returns true if clicked.
_MCQ_CLICK_JS = r"""(idx) => {
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const els = [...document.querySelectorAll('.option-lable')].filter(vis);
  if (idx<0 || idx>=els.length) return false;
  const el = els[idx];
  el.scrollIntoView({block:'center'});
  (el.querySelector('input[type=radio],input[type=checkbox]') || el).click();
  return true;
}"""

# Typing module: extract the reference SENTENCE to copy (the prominent text that is NOT the
# instruction and NOT inside an input) + the input element presence. The accuracy test only advances
# when the shown sentence is typed, so we must copy it exactly (via real keystrokes).
_TYPING_EXTRACT_JS = r"""() => {
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const instr = /type the given sentence|exactly as shown|space provided|type the (paragraph|text)|as fast|as accurately/i;
  const inputs = [...document.querySelectorAll('textarea, [contenteditable=true], input[type=text]')].filter(vis);
  const inInput = t => inputs.some(i => (i.value||i.textContent||'').replace(/\s+/g,' ').trim() === t);
  // The sentence is usually a bordered/highlighted block; try known-ish containers first, then any
  // prominent line that reads like a sentence (has spaces + letters), excluding the instruction.
  let cands = [...document.querySelectorAll('.typing-text,.type-text,.typing-para,.given-sentence,.sentence,.paraText,.para-text,.text-to-type,.svar-las-question-block p,.svar-las-question-block')]
     .filter(vis).map(T).filter(x => x && x.length>=15 && !instr.test(x) && !inInput(x));
  if (!cands.length) {
     cands = [...document.querySelectorAll('p,div,span,label,h2,h3')].filter(vis).map(T)
        .filter(x => x && x.length>=20 && /\s/.test(x) && /[a-z]/i.test(x) && !instr.test(x) && !inInput(x));
  }
  cands.sort((a,b) => b.length - a.length);
  return {sent: cands[0]||'', nInputs: inputs.length,
          hasCta: !![...document.querySelectorAll('.primary-cta-btn')].find(e => vis(e)
                    && !/\bdisabled\b/.test(e.className||'') && e.getAttribute('aria-disabled')!=='true')};
}"""

# Focus the visible typing input (so page.keyboard.type lands there). Returns true if focused.
_TYPING_FOCUS_JS = r"""() => {
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const el = [...document.querySelectorAll('textarea, [contenteditable=true], input[type=text]')].find(vis);
  if (!el) return false;
  el.scrollIntoView({block:'center'}); el.focus();
  if (el.isContentEditable) { el.textContent=''; } else if ('value' in el) { el.value=''; }
  return true;
}"""

# Computer-proficiency / WriteX module = an Adobe Captivate (ex-Flash) SOFTWARE SIMULATION ("Print the
# current document." etc., N questions, a `.captivateIframe`). Flash is dead in modern Chromium so the
# sim never renders (no answerable options) — SKIP each question (confirming the skip-warning) to reach
# the next module. Not harvest-valuable (a simulation task, not a Q&A).
_CAPTIVATE_DETECT_JS = r"""() => {
  const b = document.body.innerText || '';
  const cap = !!document.querySelector('.captivateIframe, [class*="captivate"]') || /adobe flash player/i.test(b);
  const q = /question\s+\d+\s+out of\s+\d+/i.test(b);
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden'; };
  const skip = [...document.querySelectorAll('button,a,[role=button],.footerBtn')].some(e => vis(e) && /^skip$/i.test(T(e)));
  return cap && q && skip;
}"""
_CAPTIVATE_SKIP_JS = r"""() => {
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden'; };
  const sk = [...document.querySelectorAll('button,a,[role=button],.footerBtn,.btn-green-bs')].filter(vis)
     .find(e => /^skip$/i.test(T(e)));
  if (sk) { sk.scrollIntoView({block:'center'}); sk.click(); return true; }
  return false;
}"""
# The skip-warning ngDialog confirm ("OK").
_CAPTIVATE_OK_JS = r"""() => {
  const T = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
     return r.width>2 && r.height>2 && s.display!=='none' && s.visibility!=='hidden'; };
  const ok = [...document.querySelectorAll('.ngdialog button, .ngdialog a, .ngdialog .footerBtn, .ngdialog .btn-green-bs, button, a, [role=button]')]
     .filter(vis).find(e => /^ok$/i.test(T(e)));
  if (ok) { ok.click(); return true; }
  return false;
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
        self._typing_dumped = False    # one-time typing-DOM capture for verification
        self._mystery_dumped = False   # one-time capture of an unrecognised module (e.g. personality)
        self._pers_dumped = False      # one-time capture of a personality item (option/submit DOM)
        self._free_wav = None          # lazily-generated long passage for a free-speech window

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
        # Section C "Listening Comprehension" (+ any AMCAT MCQ) renders options as `.option-lable`,
        # which the generic reader misses -> the page looks option-less and was mis-routed to speaking.
        # Detect them FIRST so the core answers + banks it as a normal MCQ (listening if a clip is present).
        if not real_opts:
            try:
                mcq = await page.evaluate(_MCQ_OPTS_JS)
            except Exception:
                mcq = None
            if mcq and len(mcq.get("opts") or []) >= 2:
                it = dict(it)
                it["options"] = mcq["opts"]
                if mcq.get("q"):
                    it["question"] = mcq["q"]
                it["has_audio"] = bool(mcq.get("hasAudio"))
                it["has_mic"] = it["has_textarea"] = it["has_video"] = False
                it["qimgs"] = it.get("qimgs") or []
                return it
        # SVAR "Read and Speak": a real sentence prompt + svar chrome, no MCQ options -> speaking.
        # (The Section-A intro carries instructional prose, not a sentence -> excluded by _SVAR_INTRO_RE.)
        if not real_opts:
            sv = await self.svar_state(page)
            p = (sv.get("prompt") or "").strip()
            if sv.get("isSvar"):
                # Route EVERY SVAR page (read-aloud sentences, Section-B/C audio-only listen items, and
                # section intros) to handle_speaking so the core walks them (forward-first CTA). The
                # question audio is captured + transcribed by the ASR post-pass, so audio-only items and
                # intros are marked _walk_only (advanced but NOT banked here as empty speaking).
                it = dict(it)
                it["options"] = []
                it["has_mic"] = True
                it["has_textarea"] = it["has_audio"] = it["has_video"] = False
                it["qimgs"] = []
                it["_svar"] = sv
                it["question"] = p
                if not (len(p) >= 6 and not _SVAR_INTRO_RE.search(p)):
                    it["_walk_only"] = True     # intro / audio-only listen item -> walk, don't bank
                return it
        if _GATE_RE.search(it.get("body") or ""):
            it = dict(it)
            it["options"] = []
            it["has_mic"] = it["has_textarea"] = it["has_audio"] = it["has_video"] = False
            it["qimgs"] = []
        # One-time capture of an UNRECOGNISED module page (no options detected, not svar/gate, but a real
        # countdown running) so its option DOM can be handled — e.g. the Personality / cognitive modules.
        if (not real_opts and not it.get("has_mic") and not it.get("has_textarea")
                and not _GATE_RE.search(it.get("body") or "") and not self._mystery_dumped):
            body = it.get("body") or ""
            if re.search(r"\b\d{1,2}\s*:\s*\d{2}\b", body):    # a MM:SS timer -> inside a module
                self._mystery_dumped = True
                logger.info("[amcat] MYSTERY module page (no options) body=%r", body[:160])
                await self._dump_stuck(page, "mystery_module")
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

    async def _skip_captivate(self, page) -> bool:
        """SKIP one question of the Adobe Captivate (Flash) simulation module + confirm the skip-warning.
        Returns True if a skip was performed (so the core keeps walking through the whole module)."""
        try:
            if not await page.evaluate(_CAPTIVATE_DETECT_JS):
                return False
        except Exception:
            return False
        try:
            if not await page.evaluate(_CAPTIVATE_SKIP_JS):
                return False
        except Exception:
            return False
        await page.wait_for_timeout(900)
        try:
            await page.evaluate(_CAPTIVATE_OK_JS)     # confirm the "you chose to skip" ngDialog
        except Exception:
            pass
        await page.wait_for_timeout(1200)
        logger.info("[amcat] captivate/flash sim: skipped a question")
        return True

    async def advance(self, page) -> bool:
        # A dead Flash/Captivate software-simulation module -> skip through it to the next module.
        if await self._skip_captivate(page):
            return True
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
        # Resume-safety: if a token RESUMES past the diagnostic (already inside a scored module — a
        # "Question N out of M" / SVAR / a running MM:SS timer with no diagnostic gate), mark the
        # diagnostic done so the gate never re-clicks `#submit1` (which would log the session out).
        try:
            body = await page.inner_text("body", timeout=3000)
        except Exception:
            body = ""
        if (not self._diag_submitted and not _GATE_RE.search(body)
                and (re.search(r"question\s+\d+\s+out of\s+\d+", body, re.I)
                     or re.search(r"\b[0-5]?\d\s*:\s*[0-5]\d\b", body)
                     or await self.svar_state(page) != {})):
            if re.search(r"question\s+\d+\s+out of\s+\d+", body, re.I) or (await self.svar_state(page)).get("isSvar"):
                self._diag_submitted = True
                logger.info("[amcat] resume past diagnostic -> diag_submitted=True")
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

    async def handle_speaking(self, page, record_secs: float = 6.0, mic_say_wav: str | None = None) -> bool:
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
        play = None     # PASS mode: current paplay Popen feeding mic_say_wav into the virtual mic
        spoke = False   # whether we've already spoken once into the CURRENT (non-free) record window
        spoke_tick = 0  # the tick we spoke on (delay the SUBMIT click so the recorder captures speech)

        def _stop_play():
            nonlocal play
            if play is not None:
                try:
                    play.terminate()
                except Exception:
                    pass
                play = None
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
            # Section C "Listening Comprehension" is listen-FIRST: its `.option-lable` MCQ options render
            # only AFTER the audio, so read_item saw none and mis-routed here. The moment options appear,
            # BAIL (return True) so the core re-reads and answers it via the MCQ path (we can't answer an
            # MCQ by recording). Nothing to bank is lost — the item re-reads immediately.
            try:
                mcq = await page.evaluate(_MCQ_OPTS_JS)
            except Exception:
                mcq = None
            if mcq and len(mcq.get("opts") or []) >= 2:
                _stop_play()
                logger.info("[amcat] svar -> MCQ options appeared, bailing to MCQ path nav=%s", nav)
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
                    _stop_play()                 # re-speak into the mic on the next record attempt
                    spoke = False
                    logger.info("[amcat] svar WARN patient TRY AGAIN (%ds) nav=%s", warn_ticks * 3, nav)
                await page.wait_for_timeout(3000)
                continue
            warn_ticks = 0

            # ---- PASS mode: feed the required sentence into the virtual mic THROUGHOUT the record phase
            #      (rec=True) so a read-aloud / listen-repeat item is answered. Load-robust: (re)start
            #      paplay whenever it isn't running while recording, so audio is present no matter when
            #      the record window opens; stop it once we leave the window. This does NOT `continue` —
            #      it falls through to the CTA-click block, which walks the OK/SUBMIT/NEXT cycle that
            #      actually ADVANCES the item (skipping the click was what stalled Q1 forever). ----
            free_speech = bool(_FREE_SPEECH_RE.search(
                (cur.get("section") or "") + " " + (cur.get("prompt") or "")))
            feed_wav = mic_say_wav
            if free_speech:
                if self._free_wav is None:            # a long continuous passage for the speaking window
                    self._free_wav = os.path.join(
                        os.environ.get("AMCAT_DUMP_DIR", "/tmp"), "amcat_free_speech.wav")
                    try:
                        assets.speak_text_wav(_FREE_SPEECH_TEXT, self._free_wav)
                    except Exception:
                        self._free_wav = ""
                feed_wav = self._free_wav or mic_say_wav
            if cur.get("recording"):
                if free_speech:
                    # free speech: keep audio present for the whole (long) speaking window
                    if feed_wav and (play is None or play.poll() is not None):
                        play = mic.speak(feed_wav)
                        logger.info("[amcat] mic feed (free) during record nav=%s", nav)
                elif feed_wav and not spoke:
                    # listen-repeat / read-aloud: speak the sentence ONCE, then let the mic go SILENT so
                    # the recorder detects end-of-speech and ENABLES submit. Continuous audio kept SUBMIT
                    # disabled forever (the Q20 Section-B stall) because the utterance never "ended".
                    play = mic.speak(feed_wav)
                    spoke = True
                    spoke_tick = tick
                    logger.info("[amcat] mic feed (once) during record nav=%s", nav)
                # after speaking, give the audio time to play + be captured before SUBMITting — an early
                # SUBMIT (it's enabled from the record start) submits an empty clip -> "unable to hear" WARN.
                if spoke and (tick - spoke_tick) < 3:
                    await page.wait_for_timeout(1500)
                    continue
            else:
                spoke = False                          # left the record window -> next window re-speaks
                if play is not None:
                    _stop_play()

            # ---- FREE SPEECH: never SUBMIT while recording (early click -> "unable to hear" loop).
            #      Feed the mic + let the timed window run; submit only once recording has ended. ----
            if free_speech and cur.get("recording"):
                await page.wait_for_timeout(3000)
                continue

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

    async def answer_mcq(self, page, item: dict, index: int) -> bool:
        """Answer an AMCAT MCQ. Section C options are `.option-lable`; after selecting one the SUBMIT
        ANSWER button (`#submit1`, class `.primary-cta-btn`) enables — click it via the SVAR primary
        clicker (the gate's `advance` REFUSES `#submit1` to avoid the diagnostic-logout, so it can't
        submit a question). Falls back to the generic reader for any non-`.option-lable` MCQ."""
        if not self._pers_dumped and re.search(
                r"agree|describes you|which statement|\bi (am|prefer|enjoy|like|tend|find|get|feel)\b|"
                r"strongly (agree|disagree)|to what extent|how (often|much) do you",
                (item.get("question") or ""), re.I):
            self._pers_dumped = True
            logger.info("[amcat] personality item q=%r opts=%d — dumping DOM",
                        (item.get("question") or "")[:70], len(item.get("options") or []))
            await self._dump_stuck(page, "personality_first")
        try:
            ok = await page.evaluate(_MCQ_CLICK_JS, index)
        except Exception:
            ok = False
        if not ok:
            return await super().answer_mcq(page, item, index)
        await page.wait_for_timeout(700)              # let SUBMIT ANSWER enable
        try:
            await page.evaluate(_SVAR_CLICK_PRIMARY_JS)   # click the now-enabled SUBMIT ANSWER
        except Exception:
            pass
        await page.wait_for_timeout(900)
        return True

    async def handle_typing(self, page, text: str) -> bool:
        """AMCAT Typing module = a TIMED module (`<textarea ng-model=model.editor .typingTextArea>`, an
        Angular countdown ~3min): type the shown PARAGRAPH with REAL keystrokes (Angular updates on key
        events; setting `.value` is ignored), then WAIT for the module to END — it auto-advances at
        timer=0, or a SUBMIT CTA appears once done. Poll until the page leaves the typing module (handles
        multiple paragraphs + an offered submit). Returns True once it advanced, else False (churn)."""
        async def _type_current() -> str:
            try:
                info = await page.evaluate(_TYPING_EXTRACT_JS)
            except Exception:
                info = None
            para = ((info or {}).get("sent") or "").strip()
            if not self._typing_dumped:
                self._typing_dumped = True
                logger.info("[amcat] typing extract sent=%r nInputs=%s hasCta=%s",
                            para[:80], (info or {}).get("nInputs"), (info or {}).get("hasCta"))
                await self._dump_stuck(page, "typing_first")
            if not para or len(para) < 8:
                para = text
            try:
                if await page.evaluate(_TYPING_FOCUS_JS):
                    await page.keyboard.type(para[:1400], delay=6)   # real keys; Angular ng-model picks up
            except Exception:
                pass
            return para

        async def _still_typing() -> bool:
            try:
                return bool(await page.evaluate(
                    "() => !!document.querySelector('.typingModule, .typingTextArea, .typingOption')"))
            except Exception:
                return True

        last = await _type_current()
        for _ in range(80):                     # ~240s: cover the ~3-min module timer
            await page.wait_for_timeout(3000)
            if await self.is_done(page) or not await _still_typing():
                logger.info("[amcat] typing module ended -> advancing")
                return True
            try:
                cur = await page.evaluate(_TYPING_EXTRACT_JS)
            except Exception:
                cur = None
            if cur and cur.get("hasCta"):        # a submit/next offered once done -> click it
                try:
                    await page.evaluate(_SVAR_CLICK_PRIMARY_JS)
                except Exception:
                    pass
                await page.wait_for_timeout(2500)
                if not await _still_typing():
                    return True
            elif cur and cur.get("sent") and cur.get("sent") != last:   # a NEW paragraph -> type it too
                last = cur.get("sent")
                try:
                    if await page.evaluate(_TYPING_FOCUS_JS):
                        await page.keyboard.type((last or "")[:1400], delay=6)
                except Exception:
                    pass
        return False

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
