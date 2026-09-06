"""Execution adapter for explicitly allowed QA staging origins and qa-v1 pages."""
import json
from qa_bot.adapters.browser.controller import origin
from qa_bot.perception.extractor import QuestionExtractor


class StagingBrowser:
    def __init__(self, controller, staging_origins):
        self.controller = controller
        self.origins = frozenset(origin(o) for o in staging_origins)
        if any("aspiringminds.com" in o for o in self.origins):
            raise ValueError("production assessment origin is not staging")

    async def _page(self):
        page = self.controller._page
        if page is None or origin(page.url) not in self.origins:
            raise ValueError("not an authorized staging page")
        if await page.locator('[data-qa-environment="staging"]').count() != 1:
            raise ValueError("staging marker missing")
        return page

    async def current(self):
        await self._page()
        result = QuestionExtractor().extract(await self.controller.observe())
        if not result.ready:
            raise ValueError("staging question not extractable")
        return result.spec

    async def select(self, ids):
        page = await self._page()
        q = await self.current()
        if not set(ids) <= {o.id for o in q.options}:
            raise ValueError("unknown selection")
        for o in q.options:
            control = page.locator(f'[data-option-id={json.dumps(o.id)}] input')
            if o.id in ids:
                await control.check()
            elif await control.get_attribute("type") == "checkbox":
                await control.uncheck()

    async def selected(self):
        page = await self._page()
        return await page.locator("[data-option-id]").evaluate_all(
            "nodes => nodes.filter(n => n.querySelector('input')?.checked).map(n => n.dataset.optionId)")

    async def fill(self, text):
        page = await self._page()
        await page.locator('[data-field="response"] textarea').fill(text)

    async def value(self):
        page = await self._page()
        return await page.locator('[data-field="response"] textarea').input_value()

    async def navigate(self, direction):
        if direction not in ("next", "back", "submit"):
            raise ValueError("unsupported navigation")
        page = await self._page()
        await page.locator(f'[data-nav={json.dumps(direction)}]').click()

    async def accept_sandbox_consent(self):
        """Exercise a consent transition only on an explicit local QA fixture."""
        page = await self._page()
        marker = page.locator('[data-qa-sandbox-consent="true"]')
        if await marker.count() != 1:
            raise ValueError("sandbox consent marker missing")

        toggle = page.locator('[data-consent-toggle]')
        proceed = page.locator('[data-nav="continue"]')
        if await toggle.count() != 1 or await proceed.count() != 1:
            raise ValueError("sandbox consent controls missing")
        if await toggle.get_attribute("aria-checked") != "false":
            raise ValueError("sandbox consent must start at No")
        if await proceed.is_enabled():
            raise ValueError("Continue must start disabled")

        await toggle.click()
        if await toggle.get_attribute("aria-checked") != "true":
            raise RuntimeError("sandbox consent did not switch to Yes")
        if not await proceed.is_enabled():
            raise RuntimeError("Continue did not become enabled")

        await proceed.click()
        await page.wait_for_url("**/consent-complete.html")
