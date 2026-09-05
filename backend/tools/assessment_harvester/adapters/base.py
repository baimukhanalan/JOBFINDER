"""Adapter protocol + generic implementations the core loop calls.

A platform adapter subclasses `Adapter` and overrides what differs. The generic defaults here are
deliberately broad (they work on many assessment SPAs): a whole-page item reader that finds the
question text + visible answer options + media/free-response flags, a forward-control clicker, and a
noise/modal dismisser. Per-platform adapters (e.g. AMCAT) override `read_item`/`answer_mcq`/`enter`
with real selectors once the live DOM is known.
"""
from __future__ import annotations

import re

# Chrome lines that are never the question or an option.
_NAV_RE = re.compile(
    r"^(skip to main content|exit|question|back|help|settings|next|continue|proceed|previous|"
    r"select language|accessibility.*|sign out|log ?out|tips|get started!?|submit|start|begin|"
    r"finish|©.*|\d+\s*%|\d+\s*/\s*\d+|logout|home|menu)$", re.I)

_FORWARD_RE = re.compile(
    r"^(next|continue|proceed|start|begin|launch|go|save (?:& |and )?(?:next|continue)|"
    r"start assessment|begin assessment|start test|begin test|get started|start now|"
    r"i'?m ready|ready|ok|submit|submit test|finish)$", re.I)
_FORWARD_DENY_RE = re.compile(
    r"sign\s*out|log\s*out|\bexit\b|cancel|\bhelp\b|previous|\bback\b|restart|instructions", re.I)

_DISMISS_NAMES = ("Close", "OK", "Okay", "Got it", "Got It", "Dismiss", "I understand",
                  "Acknowledge", "Continue", "Proceed", "Allow", "Accept", "Accept All")

# Generic item reader: question text + visible options (radio labels / role=radio / list items /
# clickable option divs), question images, and media / free-response flags. Returns raw signals;
# classification happens in core.
_GENERIC_ITEM_JS = r"""() => {
  const T = el => (el.textContent || '').replace(/\s+/g,' ').trim();
  const vis = el => { const r = el.getBoundingClientRect();
     const s = getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const NAV = /^(skip to main content|exit|question|back|help|settings|next|continue|proceed|previous|select language|accessibility.*|sign out|log ?out|tips|get started!?|submit|start|begin|finish|©.*|\d+\s*%|\d+\s*\/\s*\d+|logout|home|menu)$/i;

  // ---- candidate OPTION elements (several patterns) ----
  let optEls = [];
  const pushAll = sel => document.querySelectorAll(sel).forEach(e => { if (vis(e)) optEls.push(e); });
  // radio inputs -> their label (or parent)
  document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(inp => {
    let lab = null;
    if (inp.id) lab = document.querySelector('label[for="'+CSS.escape(inp.id)+'"]');
    if (!lab) lab = inp.closest('label');
    if (!lab) lab = inp.parentElement;
    if (lab && vis(lab)) optEls.push(lab);
  });
  ['[role=radio]','label.question-answer-label','.option','.answer-option','.mcq-option',
   'li.option','.options li','[class*="option"] label','.ans-option','[data-option]'
  ].forEach(pushAll);
  // dedup by element, keep DOM order, filter chrome/empty
  const seen = new Set(); const options = [];
  optEls.forEach(e => {
    if (seen.has(e)) return; seen.add(e);
    const txt = T(e);
    const img = e.querySelector('img');
    if (!txt && !img) return;
    if (txt && NAV.test(txt)) return;
    if (txt && txt.length > 400) return;   // paragraph, not an option
    options.push({text: txt, image: img ? (img.src||img.getAttribute('src')||'') : null});
  });

  // ---- question text: longest visible line that isn't an option or chrome ----
  const optTexts = new Set(options.map(o => (o.text||'').trim()));
  const lines = (document.body.innerText||'').split('\n').map(s => s.trim()).filter(Boolean);
  const cand = lines.filter(l => !optTexts.has(l) && !NAV.test(l) && l.length > 8);
  cand.sort((a,b) => b.length - a.length);
  const question = (cand[0] || '').slice(0, 1200);

  // ---- question-region images (for media_sig) ----
  const qimgs = [];
  document.querySelectorAll('img').forEach(im => {
    if (!vis(im)) return;
    const r = im.getBoundingClientRect();
    if (r.width < 24 || r.height < 24) return;      // skip icons/logos
    const src = im.src || im.getAttribute('src') || '';
    if (src && !/logo|icon|avatar|sprite/i.test(src)) qimgs.push(src);
  });

  // ---- media / free-response flags ----
  const has_table = document.querySelectorAll('table').length > 0;
  const textareas = [...document.querySelectorAll('textarea, [contenteditable=true], [role=textbox]')].filter(vis);
  const bigText = textareas.filter(e => { const r=e.getBoundingClientRect(); return r.height>28; });
  const hasVideo = [...document.querySelectorAll('video')].some(vis);
  const audioEls = [...document.querySelectorAll('audio')].some(vis);
  // a "record" / mic control -> speaking/SVAR
  let hasMic = false;
  document.querySelectorAll('button,[role=button],a,div,span').forEach(b => {
    if (!vis(b)) return;
    const s = ((b.getAttribute('aria-label')||'') + ' ' + (b.className||'') + ' ' + T(b)).toLowerCase();
    if (/\brecord\b|start recording|microphone|\bmic\b|record answer|record response/.test(s)) hasMic = true;
  });

  const pm = (document.body.innerText||'').match(/(\d+)\s*%/);
  const it = document.body.innerText || '';
  return {
    question, options, qimgs, has_table,
    has_textarea: bigText.length > 0,
    has_video: hasVideo, has_audio: audioEls, has_mic: hasMic,
    progress: pm ? parseInt(pm[1],10) : null,
    body: it.replace(/\s+/g,' ').trim().slice(0, 600),
    url: location.href,
    n_iframes: document.querySelectorAll('iframe').length,
  };
}"""


class Adapter:
    platform = "base"

    async def enter(self, page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        await self.dismiss_noise(page)

    async def read_item(self, page) -> dict:
        # Try the main frame, then any child frame that yields options (SPAs often iframe the player).
        best = await self._read_frame(page)
        if best.get("options") or best.get("has_textarea") or best.get("has_mic"):
            return best
        for fr in page.frames:
            if fr is page.main_frame:
                continue
            try:
                d = await fr.evaluate(_GENERIC_ITEM_JS)
            except Exception:
                continue
            if d.get("options") or d.get("has_textarea") or d.get("has_mic"):
                d["_frame_url"] = fr.url
                return d
        return best

    async def _read_frame(self, page) -> dict:
        try:
            return await page.evaluate(_GENERIC_ITEM_JS)
        except Exception:
            return {"question": "", "options": [], "qimgs": [], "has_table": False,
                    "has_textarea": False, "has_video": False, "has_audio": False,
                    "has_mic": False, "progress": None, "body": "", "url": page.url}

    async def dismiss_noise(self, page) -> bool:
        for name in _DISMISS_NAMES:
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I))
                if await b.count():
                    el = b.first
                    if await el.is_visible() and await el.is_enabled():
                        await el.click(timeout=2000)
                        await page.wait_for_timeout(700)
                        return True
            except Exception:
                continue
        return False

    async def _forward_locator(self, page):
        try:
            b = page.get_by_role("button", name=_FORWARD_RE)
            n = await b.count()
        except Exception:
            n = 0
        for i in range(n - 1, -1, -1):
            el = b.nth(i)
            try:
                nm = (await el.inner_text()) or (await el.get_attribute("aria-label")) or ""
                if _FORWARD_DENY_RE.search(nm):
                    continue
                if await el.is_visible() and await el.is_enabled():
                    return el
            except Exception:
                continue
        # links
        try:
            lk = page.get_by_role("link", name=_FORWARD_RE)
            if await lk.count():
                el = lk.first
                if await el.is_visible():
                    return el
        except Exception:
            pass
        return None

    async def advance(self, page) -> bool:
        el = await self._forward_locator(page)
        if not el:
            return False
        try:
            await el.click(timeout=3000)
            await page.wait_for_timeout(2000)
            return True
        except Exception:
            return False

    async def is_done(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            body = ""
        return bool(re.search(
            r"you have (completed|finished)|assessment (complete|finished|submitted)|"
            r"thank you for (completing|taking)|successfully (completed|submitted)|"
            r"test (completed|finished|submitted)|all done|you may now close|"
            r"no (further|more) questions", body))

    async def wall(self, page) -> str | None:
        """A GENUINE, unbreachable wall the harvester should stop and report (a terminal token, a
        webcam-proctor logout, a hard captcha). None = keep going. Per-platform override."""
        return None

    async def try_skip(self, page) -> bool:
        """Try to move past a free-response item without answering (Skip / Next / 'later')."""
        for name in ("Skip", "Skip Question", "Next", "I'll do this later", "Do it later", "Later"):
            try:
                b = page.get_by_role("button", name=re.compile(rf"^{re.escape(name)}$", re.I))
                if await b.count() and await b.first.is_enabled():
                    await b.first.click(timeout=2500)
                    await page.wait_for_timeout(2000)
                    return True
            except Exception:
                continue
        return False

    async def _click_by_re(self, page, rx: re.Pattern) -> bool:
        """Click the first visible button/role=button/a whose text/aria-label/class matches rx."""
        js = r"""(pat) => {
          const re = new RegExp(pat, 'i');
          const vis = el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
             return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
          const els = [...document.querySelectorAll('button,[role=button],a,input[type=submit],input[type=button]')];
          for (const b of els) {
            if (!vis(b)) continue;
            const s = ((b.getAttribute('aria-label')||'')+' '+(b.className||'')+' '+(b.value||'')+' '+(b.innerText||'')).trim();
            if (re.test(s)) { b.scrollIntoView({block:'center'}); b.click(); return true; }
          }
          return false;
        }"""
        for target in [page] + [f for f in page.frames if f is not page.main_frame]:
            try:
                if await target.evaluate(js, rx.pattern):
                    return True
            except Exception:
                continue
        return False

    async def audio_url(self, page) -> str | None:
        """Best-effort: the src of an <audio>/<source> or a data-audio attribute in any frame."""
        js = r"""() => {
          const a = document.querySelector('audio source[src], audio[src], [data-audio], [data-audio-url]');
          if (!a) return null;
          return a.getAttribute('src') || a.getAttribute('data-audio') || a.getAttribute('data-audio-url');
        }"""
        for target in [page] + [f for f in page.frames if f is not page.main_frame]:
            try:
                u = await target.evaluate(js)
                if u:
                    return u
            except Exception:
                continue
        return None

    async def handle_listening(self, page) -> str | None:
        """A listening item: audio autoplays (autoplay-policy relaxed). Grab the audio URL, nudge a
        Play control if present, wait for it to run, and return the URL. The MCQ that follows is
        answered by the normal core path."""
        url = await self.audio_url(page)
        await self._click_by_re(page, re.compile(r"\bplay\b|play audio|listen|start audio"))
        await page.wait_for_timeout(4000)  # let it play; fake pipeline has no real duration gate
        return url

    async def handle_speaking(self, page, record_secs: float = 6.0) -> bool:
        """Run the record -> stop -> submit cycle so an SVAR/speaking item ADVANCES. The fake audio
        device feeds speech.wav, so the recorder captures audible input. Returns True if it looks
        like it advanced (a submit/next control was clicked)."""
        started = await self._click_by_re(page, re.compile(r"\brecord\b|start recording|record answer|record response|\bmic\b|microphone"))
        if not started:
            return False
        await page.wait_for_timeout(int(record_secs * 1000))
        await self._click_by_re(page, re.compile(r"\bstop\b|stop recording|finish recording|done recording"))
        await page.wait_for_timeout(1500)
        # submit/save the recording, then next
        submitted = await self._click_by_re(page, re.compile(r"\bsubmit\b|save (answer|response|recording)|\bsave\b|\bnext\b|\bcontinue\b|\bdone\b|\bproceed\b"))
        await page.wait_for_timeout(1500)
        return started or submitted

    async def handle_typing(self, page, text: str) -> bool:
        """Fill a free-text/typing item with `text` and advance. For a typing-SPEED test this may be
        flagged, but it lets the item advance so we can harvest what follows."""
        js = r"""(txt) => {
          const vis = el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
             return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
          const el = [...document.querySelectorAll('textarea,[contenteditable=true],[role=textbox],input[type=text]')].find(vis);
          if (!el) return false;
          el.focus();
          if (el.isContentEditable) { el.textContent = txt; }
          else { el.value = txt; }
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return true;
        }"""
        ok = False
        for target in [page] + [f for f in page.frames if f is not page.main_frame]:
            try:
                if await target.evaluate(js, text):
                    ok = True
                    break
            except Exception:
                continue
        if ok:
            await page.wait_for_timeout(500)
            await self.advance(page)
        return ok

    async def answer_mcq(self, page, item: dict, index: int) -> bool:
        """Click the visible option at `index`. Generic: re-derive the option elements the same way
        the reader did, in DOM order, and click the nth. Overridden per-platform when needed."""
        js = r"""(idx) => {
          const T = el => (el.textContent||'').replace(/\s+/g,' ').trim();
          const vis = el => { const r = el.getBoundingClientRect(); const s=getComputedStyle(el);
             return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
          const NAV = /^(next|continue|proceed|previous|back|submit|start|begin|finish|exit)$/i;
          let optEls = [];
          const push = sel => document.querySelectorAll(sel).forEach(e => { if (vis(e)) optEls.push(e); });
          document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(inp => {
            let lab=null; if (inp.id) lab=document.querySelector('label[for="'+CSS.escape(inp.id)+'"]');
            if (!lab) lab=inp.closest('label'); if (!lab) lab=inp.parentElement;
            if (lab && vis(lab)) optEls.push(lab); });
          ['[role=radio]','label.question-answer-label','.option','.answer-option','.mcq-option',
           'li.option','.options li','[class*="option"] label','.ans-option','[data-option]'
          ].forEach(push);
          const seen=new Set(); const opts=[];
          optEls.forEach(e => { if (seen.has(e)) return; seen.add(e);
            const txt=T(e); const img=e.querySelector('img');
            if (!txt && !img) return; if (txt && NAV.test(txt)) return; if (txt && txt.length>400) return;
            opts.push(e); });
          if (idx<0 || idx>=opts.length) return false;
          const el = opts[idx];
          el.scrollIntoView({block:'center'});
          const inp = el.querySelector('input[type=radio],input[type=checkbox]')
                   || (el.matches('input') ? el : null);
          (inp || el).click();
          return true;
        }"""
        # click in whichever frame has the options
        for target in [page] + [f for f in page.frames if f is not page.main_frame]:
            try:
                ok = await target.evaluate(js, index)
                if ok:
                    await page.wait_for_timeout(600)
                    return True
            except Exception:
                continue
        return False
