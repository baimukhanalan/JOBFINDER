"""Ashby ATS pre-fill strategy.

Ashby hosts the application form at jobs.ashbyhq.com/{org}/{id} as a React SPA.
No login is required to apply; the Apply button typically reveals the form inline
rather than navigating away, so we click it (when gated) and let the base prefill
wait for the React-rendered fields to mount.
"""
from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy


class AshbyStrategy(ApplyStrategy):
    name = "ashby"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "ashbyhq.com" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        # A scraped posting URL (jobs.ashbyhq.com/{org}/{id}) is NOT the form — the form
        # lives at {id}/application. The live API gives that directly; scraped URLs don't.
        url = page.url.split("?")[0]
        if "/application" not in url:
            try:
                await page.goto(url.rstrip("/") + "/application",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)  # React SPA: let fields mount
            except Exception:
                pass
        # Fallback: some views still gate the form behind an Apply button (reveals inline).
        for sel in [
            'a:has-text("Apply for this Job")', 'button:has-text("Apply for this Job")',
            'button:has-text("Apply")', 'a:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    async def autofill_from_resume(self, page: Page, resume_path: str) -> bool:
        """Upload the résumé to Ashby's "Autofill from resume" input (a document-only
        file input next to that label, distinct from the `_systemfield_resume`
        attachment) so Ashby's parser pre-populates name/email/experience. Waits for
        the async parse to land before the analyzer fills the rest."""
        if not resume_path:
            return False
        try:
            for inp in await page.query_selector_all('input[type="file"]'):
                info = await inp.evaluate(
                    '(el)=>{const c=el.closest("div,section,form");'
                    'return {id:(el.id||""), acc:(el.accept||""),'
                    ' near:((c?c.innerText:"")||"").toLowerCase()};}')
                is_autofill = ("autofill" in (info.get("near") or "")
                               and "_systemfield_resume" not in (info.get("id") or "")
                               and "image/" not in (info.get("acc") or ""))
                if is_autofill:
                    await inp.set_input_files(resume_path)
                    # Ashby parses server-side then RE-RENDERS its controlled form state,
                    # sometimes in MORE THAN ONE async pass. If our field fill runs between
                    # passes, a later pass unbinds it from React state — the Cohere
                    # "needs correction / Missing entry" bug, where a Yes/No screener button
                    # still SHOWS selected but its value was wiped. The old wait returned as
                    # soon as ONE text field populated (+1500ms), often BEFORE the screener
                    # re-render, so our subsequent fill got clobbered. Now wait until the form
                    # is STABLE: a field populated, no "parsing/pending" indicator visible, and
                    # the value+button-toggle signature unchanged across two reads (every
                    # re-render pass has landed) — so the analyzer fills onto a settled form.
                    _SNAP_JS = (
                        '()=>{const f=[...document.querySelectorAll("input,textarea")];'
                        'const filled=f.some(e=>["text","email","tel",""].includes('
                        '(e.type||"").toLowerCase()) && (e.value||"").trim().length>0);'
                        'const sig=f.map(e=>e.value||"").join("|")+"#"+'
                        '[...document.querySelectorAll("button,[role=radio],[role=checkbox]")]'
                        '.map(b=>(b.getAttribute("aria-pressed")||"")+(b.getAttribute("aria-checked")||"")'
                        '+(b.getAttribute("data-state")||"")).join("");'
                        'const pending=[...document.querySelectorAll("[data-state],[class*=pending],[class*=parsing]")]'
                        '.some(e=>{const st=(e.getAttribute("data-state")||"").toLowerCase();'
                        'const cn=(e.className||"").toString().toLowerCase();'
                        'return e.offsetParent!==null && (st==="pending" || (/pending|parsing/.test(cn) && st!=="hidden"));});'
                        'return {filled, pending, sig};}')
                    prev = None
                    stable = 0
                    landed = False
                    for _ in range(20):
                        await page.wait_for_timeout(1000)
                        try:
                            s = await page.evaluate(_SNAP_JS)
                        except Exception:
                            break
                        if not (s.get("filled") and not s.get("pending")):
                            stable = 0
                            prev = s.get("sig")
                            continue
                        landed = True
                        if prev is not None and s.get("sig") == prev:
                            stable += 1
                            if stable >= 2:  # two consecutive unchanged reads = fully settled
                                return True
                        else:
                            stable = 0
                        prev = s.get("sig")
                    # settle window exhausted — give one last beat for a trailing pass
                    await page.wait_for_timeout(1500)
                    return landed or True
        except Exception:
            return False
        return False
