"""Lever ATS pre-fill strategy.

Lever hosts applications at jobs.lever.co/{company}/{id} (the posting) with the
form at the same URL + "/apply". No login is required to apply, and field names
are consistent (name, email, phone, resume, urls[...], cards[...]), which the
rule-based analyzer handles well. Navigating to /apply is handled by the runner;
this strategy just clicks the "Apply" button if the posting gates the form.
"""
from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy


class LeverStrategy(ApplyStrategy):
    name = "lever"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "lever.co" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        # The form lives at {posting}/apply. A scraped posting URL lands on the
        # description page; navigate to /apply ourselves. Skip board-root URLs
        # (jobs.lever.co/{company} with no posting id — nothing to apply to).
        from urllib.parse import urlparse
        url = page.url.split("?")[0]
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) >= 2 and parts[-1] != "apply":
            try:
                await page.goto(url.rstrip("/") + "/apply",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            except Exception:
                pass
        # Fallback: some postings gate the form behind an "Apply" button.
        for sel in [
            'a:has-text("Apply for this job")', 'button:has-text("Apply for this job")',
            'a.postings-btn', 'a:has-text("Apply")', 'button:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue
