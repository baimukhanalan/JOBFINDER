"""SHL TalentCentral adapter (Sutherland lane, and reusable for any SHL experience link).

Entry: `talentcentral.us1.shl.com/experience/#/link/<base64-JSON>` from the "Your Sutherland
assessment invitation" email. Same SHL SPA family the Maximus etalon (`shl_assessment.py`) already
drives — so this adapter REUSES the etalon's battle-tested helpers (cookie dismiss, mandatory-consent
toggle, item reader `label.question-answer-label`, forward control, completion regex) rather than
duplicating them, and NEVER modifies the etalon. Difference from the etalon: harvest mode
(core answers RANDOMLY and banks every item), and it also carries the generic media flags so
speaking/listening/video sub-tests are handled by the fake-device path in core.
"""
from __future__ import annotations

import re

from backend.tools import shl_assessment as sa
from backend.tools.assessment_harvester.adapters.base import Adapter, _GENERIC_ITEM_JS

# Cookiebot (Usercentrics) accept + REMOVE the dialog. It overlays the page bottom AND its text
# ("This website uses cookies", Necessary/Preferences/Statistics/Marketing) pollutes the item reader,
# so after accepting we delete the dialog node outright.
_COOKIE_JS = r"""() => {
  let acted = false;
  const ids = ['CybotCookiebotDialogBodyButtonAccept',
               'CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
               'CybotCookiebotDialogBodyLevelButtonAccept'];
  for (const id of ids) { const b=document.getElementById(id); if (b) { b.click(); acted = true; break; } }
  if (!acted) {
    const cand=[...document.querySelectorAll('#CybotCookiebotDialog button, #CybotCookiebotDialog a')]
      .find(b=>/allow all|accept all|^\s*ok\s*$|^\s*accept\s*$/i.test((b.innerText||'').trim()));
    if (cand){ cand.click(); acted = true; }
  }
  // remove the lingering dialog + backdrop so it stops covering + polluting the reader
  ['CybotCookiebotDialog','CybotCookiebotDialogBodyUnderlay','CookiebotWidget'].forEach(id=>{
     const el=document.getElementById(id); if(el){ el.remove(); acted = true; }});
  return acted;
}"""

# a consent checkbox that gates the flow (welcome page "I have read and agree..."). Matched by label.
_CONSENT_LABEL_RE = "i have read|i agree|data protection|privacy notice|terms|i consent"

# The SHL/Sutherland welcome checkbox is a MUI control whose native <input type=checkbox> is visually
# hidden (opacity:0) — Playwright .check()/.click() are actionability-gated and miss it, so the flow
# stalled at "Welcome!" (Continue stays disabled until it's ticked). A JS .click() on the native input
# bypasses actionability and MUI still fires its change handler -> Continue enables. Gated to a checkbox
# whose surrounding label text is a consent phrase; never touches Cookiebot toggles.
_CONSENT_TICK_JS = r"""() => {
  const re = /i have read|i agree|data protection|privacy notice|\bterms\b|i consent/i;
  let ticked = false;
  for (const cb of document.querySelectorAll('input[type="checkbox"], [role="checkbox"]')) {
    const id = cb.id || '';
    if (id.indexOf('Cybot') === 0) continue;
    const isChecked = cb.checked === true || cb.getAttribute('aria-checked') === 'true';
    if (isChecked) continue;
    const root = cb.closest('label, .MuiFormControlLabel-root, li, div') || cb.parentElement || cb;
    const txt = (root && root.textContent) || '';
    if (!re.test(txt)) continue;
    try { cb.click(); ticked = true; } catch (e) {}
  }
  return ticked;
}"""

_MEDIA_FLAGS_JS = r"""() => {
  const vis = el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
     return r.width>2 && r.height>2 && s.visibility!=='hidden' && s.display!=='none'; };
  const qimgs=[];
  document.querySelectorAll('img').forEach(im=>{ if(!vis(im))return;
    const r=im.getBoundingClientRect(); if(r.width<24||r.height<24)return;
    const s=im.src||im.getAttribute('src')||''; if(s&&!/logo|icon|avatar|sprite/i.test(s)) qimgs.push(s); });
  const hasVideo=[...document.querySelectorAll('video')].some(vis);
  const hasAudio=[...document.querySelectorAll('audio')].some(vis) ||
     !!document.querySelector('[class*="audio"],[data-audio],button[aria-label*="play" i]');
  let hasMic=false;
  document.querySelectorAll('button,[role=button],a,div,span').forEach(b=>{ if(!vis(b))return;
    const s=((b.getAttribute('aria-label')||'')+' '+(b.className||'')+' '+(b.textContent||'')).toLowerCase();
    if(/\brecord\b|start recording|microphone|\bmic\b|record answer|record response/.test(s)) hasMic=true; });
  const bigText=[...document.querySelectorAll('textarea,[contenteditable=true],[role=textbox]')]
     .filter(e=>{const r=e.getBoundingClientRect(); return vis(e)&&r.height>28;});
  return {qimgs, has_video:hasVideo, has_audio:hasAudio, has_mic:hasMic, has_textarea:bigText.length>0};
}"""


class ShlAdapter(Adapter):
    platform = "shl_sutherland"

    async def _accept_cookies(self, page) -> bool:
        try:
            return await page.evaluate(_COOKIE_JS)
        except Exception:
            return False

    async def _tick_consent(self, page) -> bool:
        """Tick a flow-gating consent checkbox (welcome page "I have read and agree to SHL's Data
        Protection Notice"). It's a MUI checkbox whose native <input> is visually hidden, so
        Playwright .check()/.click() (actionability-gated) miss it and the flow stalled at Welcome.
        Try a JS .click() on the native input FIRST (bypasses actionability; MUI still fires change),
        then fall back to the Playwright path for older markup. Excludes Cookiebot (id^=Cybot)."""
        try:
            if await page.evaluate(_CONSENT_TICK_JS):
                return True
        except Exception:
            pass
        ticked = False
        try:
            cbs = page.get_by_role("checkbox")
            n = await cbs.count()
        except Exception:
            n = 0
        for i in range(n):
            c = cbs.nth(i)
            try:
                cid = (await c.get_attribute("id")) or ""
                if cid.startswith("Cybot"):
                    continue
                if await c.is_checked():
                    continue
                await c.check(timeout=2500)
                ticked = True
            except Exception:
                try:
                    await c.click(timeout=1500)
                    ticked = True
                except Exception:
                    continue
        return ticked

    async def enter(self, page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        for _ in range(3):
            if await self._accept_cookies(page):
                await page.wait_for_timeout(1000)
            else:
                break

    async def dismiss_noise(self, page) -> bool:
        acted = False
        if await self._accept_cookies(page):
            await page.wait_for_timeout(600)
            acted = True
        # flow-gating consent checkbox (welcome) -> tick it, then let advance() click Continue
        try:
            if await self._tick_consent(page):
                await page.wait_for_timeout(900)   # let React enable the gated Continue button
                await self.advance(page)
                return True
        except Exception:
            pass
        # info/nudge modal (Close/OK/Got it) — reuse the etalon's button-keyed dismisser
        try:
            if await sa._dismiss_modal(page):
                return True
        except Exception:
            pass
        return acted

    async def read_item(self, page) -> dict:
        # (1) etalon item reader (label.question-answer-label) across frames — Maximus-OPQ markup.
        base = {"question": "", "options": [], "has_table": False, "progress": None, "body": ""}
        for target in [page] + [f for f in page.frames if f is not page.main_frame]:
            try:
                d = await target.evaluate(sa._ITEM_JS)
            except Exception:
                continue
            if d.get("options"):
                base = d
                break
            if not base.get("body") and d.get("body"):
                base = d

        if base.get("options"):
            item = {"question": base.get("question", ""),
                    "options": [{"text": o, "image": None} for o in base["options"]],
                    "has_table": base.get("has_table", False), "progress": base.get("progress"),
                    "body": base.get("body", ""), "url": page.url, "_src": "shl"}
        else:
            # (2) fall back to the GENERIC reader (radios / role=radio / option divs) — any other
            # SHL widget markup; answer via the generic clicker.
            item = await super().read_item(page)
            item["_src"] = "generic"
            if not item.get("body"):
                item["body"] = base.get("body", "")

        item.setdefault("qimgs", [])
        for k in ("has_video", "has_audio", "has_mic", "has_textarea"):
            item.setdefault(k, False)
        try:
            flags = await page.evaluate(_MEDIA_FLAGS_JS)
            for k in ("qimgs", "has_video", "has_audio", "has_mic", "has_textarea"):
                if flags.get(k):
                    item[k] = flags[k]
        except Exception:
            pass
        return item

    async def answer_mcq(self, page, item: dict, index: int) -> bool:
        if item.get("_src") == "shl":
            try:
                if await sa._click_option(page, index):
                    return True
            except Exception:
                pass
        return await super().answer_mcq(page, item, index)

    async def advance(self, page) -> bool:
        try:
            fwd = await sa._forward_control(page)
            if fwd:
                await fwd.click(timeout=3000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        return await super().advance(page)

    async def is_done(self, page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            body = ""
        if re.search(r"\b0\s*assessment", body) and "left" in body:
            return True
        if sa._COMPLETE_RE.search(body):
            return True
        return False

    async def wall(self, page) -> str | None:
        """SHL Sutherland gates the scored assessment behind WEBCAM PROCTORING. The SHL intro
        (cookies -> Welcome[consent] -> proctoring instructions) redirects to the AMCAT / Aspiring
        Minds player whose continuous proctor issues 'Error Code WCI200 ... unable to detect a camera
        on your device ... you have been logged out' — terminal.

        RE-CONFIRMED 2026-09-13 (corrects the earlier "beaten by v4l2loopback" claim in CLAUDE.md /
        this codebase, which was over-claimed): WCI200 fires EVEN WITH a genuine, continuously-fed
        v4l2loopback `/dev/video0` that Chromium enumerates and streams — a getUserMedia probe returned
        a LIVE `Integrated Camera` track (1280x720, live, no error), and the same wall hit with a DARK
        feed AND a bright moving `testsrc2` feed, over the phone egress slot (the same egress the
        `sutherland_runner` path uses). So WCI200 is NOT a local getUserMedia/enumeration failure and
        NOT feed-brightness / egress dependent: the AMCAT proctor fingerprints and REJECTS the virtual
        camera itself (a v4l2loopback device lacks the hardware signature its native check wants). It
        is un-passable on this host without a REAL physical webcam or defeating the proctor's
        virtual-camera fingerprint (unbuilt). The rest of the Sutherland SHL INTRO is now automated end
        to end (real-camera launch hydrates the SPA, the MUI consent checkbox is JS-ticked, the loading
        handoff is waited out, item #1 is read) — only this proctor camera wall remains."""
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            return None
        if "wci200" in body or ("logged out" in body and "camera" in body):
            return "proctor_camera"
        return None
