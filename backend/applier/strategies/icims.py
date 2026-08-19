"""iCIMS ATS pre-fill strategy.

iCIMS career portals (careers-{co}.icims.com) embed the posting/form in an iframe and
gate the application behind "Apply for this job online" + usually account or SSO creation.
We click through; account-gated pages surface as login_required and stop for the human.

NOTE: iCIMS renders the form inside an iframe, which the rule-based analyzer's top-frame
locators don't pierce — so reachable fields are limited until iframe support is added.
Most iCIMS roles are account-gated anyway, so they're effectively a human-handoff lane.
"""
from playwright.async_api import Page

from backend.applier.strategies.base import ApplyStrategy


class ICIMSStrategy(ApplyStrategy):
    name = "icims"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "icims.com" in (url or "").lower()

    async def open_form(self, page: Page) -> None:
        for sel in [
            'a:has-text("Apply for this job online")', 'button:has-text("Apply for this job online")',
            'a:has-text("Apply")', 'button:has-text("Apply")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
