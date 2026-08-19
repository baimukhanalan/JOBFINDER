"""Workday ATS pre-fill strategy.

Workday hosts applications on {tenant}.myworkdayjobs.com as a multi-step wizard that
almost always requires creating or authenticating an account BEFORE the form. We click
through toward the form; when Workday gates it behind account creation / sign-in, the
analyzer reports page_type=login_required and the run stops cleanly for a human to take
over (consistent with the semi-auto, human-submit model). First-page fields that ARE
reachable get pre-filled.
"""
from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy


class WorkdayStrategy(ApplyStrategy):
    name = "workday"

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        return "myworkdayjobs.com" in u or ".workday.com" in u

    async def open_form(self, page: Page) -> None:
        # Workday's "Apply" then (sometimes) "Apply Manually" reveal the form / account gate.
        for sel in [
            'a[data-automation-id="adventureButton"]',
            'button:has-text("Apply Manually")', 'a:has-text("Apply Manually")',
            'button:has-text("Apply")', 'a:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(3000)  # multi-step SPA
                    break
            except Exception:
                continue
