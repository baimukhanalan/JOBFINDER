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
        """Tick a flow-gating consent checkbox (welcome page) via Playwright .check() — it's a MUI
        switch, so JS-click/.mandatorychk (the etalon path) doesn't flip it. Excludes the Cookiebot
        toggles (id^=Cybot). Returns True if it ticked one that was unchecked."""
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
                await page.wait_for_timeout(400)
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
        """SHL Sutherland gates the scored assessment behind WEBCAM PROCTORING (SHL smart-proctor on
        a dedicated player host). Even with a working fake camera in the SHL frame, the proctor's own
        check fails and issues 'Error Code WCI200 ... you have been logged out' — terminal. That is
        the genuine wall (a synthetic camera the proctor rejects)."""
        try:
            body = (await page.inner_text("body", timeout=3000)).lower()
        except Exception:
            return None
        if "wci200" in body or ("logged out" in body and "camera" in body):
            return "proctor_camera"
        return None
