"""Greenhouse ATS pre-fill strategy.

Greenhouse hosts the application form inline at
boards.greenhouse.io/{token}/jobs/{id} and job-boards.greenhouse.io/{token}/jobs/{id}
(also embedded on company career pages). No login is required to apply, and field
names are consistent (first_name, last_name, email, phone, resume, question_*),
which the rule-based analyzer handles well.
"""
from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy


class GreenhouseStrategy(ApplyStrategy):
    name = "greenhouse"

    @classmethod
    def matches(cls, url: str) -> bool:
        u = (url or "").lower()
        return ("greenhouse.io" in u) or ("grnh.se" in u)

    async def open_form(self, page: Page) -> None:
        # On the embedded/board view the form is usually inline; some pages gate it
        # behind an "Apply" / "Apply for this job" button.
        for sel in [
            'a:has-text("Apply for this job")', 'button:has-text("Apply for this job")',
            'a#apply_button', 'button:has-text("Apply")', 'a:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue
