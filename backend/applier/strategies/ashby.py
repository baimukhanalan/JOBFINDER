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
                    # Ashby parses server-side then fills fields. Poll until a text
                    # field actually populates (up to ~15s) instead of a fixed guess,
                    # so we capture the MAXIMUM the parser fills before returning.
                    for _ in range(15):
                        await page.wait_for_timeout(1000)
                        landed = await page.evaluate(
                            '()=>[...document.querySelectorAll("input,textarea")]'
                            '.some(e=>["text","email","tel",""].includes('
                            '(e.type||"").toLowerCase()) && (e.value||"").trim().length>0)')
                        if landed:
                            await page.wait_for_timeout(1500)  # let the rest land too
                            return True
                    return True
        except Exception:
            return False
        return False
