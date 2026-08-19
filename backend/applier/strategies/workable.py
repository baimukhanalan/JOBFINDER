"""Workable ATS pre-fill strategy.

Workable hosts the application form at apply.workable.com/{company}/j/{token}/apply,
which usually shows the form directly (no login required). Some company-hosted
views gate the form behind an "Apply" button. Field names are consistent
(firstname, lastname, email, phone, resume, question_*), which the rule-based
analyzer handles well.
"""
from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy


class WorkableStrategy(ApplyStrategy):
    name = "workable"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "workable.com" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        # apply.workable.com pages show the form inline; only some views gate it
        # behind an "Apply" / "Apply for this job" button.
        for sel in [
            'a:has-text("Apply for this job")', 'button:has-text("Apply for this job")',
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
