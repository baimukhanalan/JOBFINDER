import asyncio
import logging
import os

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from backend.config import settings

logger = logging.getLogger(__name__)


class BrowserManager:
    """Singleton Playwright browser running in headful mode on Xvfb display."""

    _instance = None
    _browser: Browser | None = None
    _context: BrowserContext | None = None
    _playwright = None

    @classmethod
    async def get_instance(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance._start()
        return cls._instance

    async def _start(self):
        os.environ["DISPLAY"] = settings.display_number
        self._playwright = await async_playwright().start()

        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,800",
        ]

        proxy = None
        if settings.apply_proxy:
            from urllib.parse import urlparse
            parsed = urlparse(settings.apply_proxy)
            proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy["username"] = parsed.username
                proxy["password"] = parsed.password or ""

        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=launch_args,
            proxy=proxy,
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # Stealth: mask automation signals
        await self._context.add_init_script("""
            // Remove webdriver flag
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Fake plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5].map(() => ({
                    name: 'Chrome PDF Plugin',
                    description: 'Portable Document Format',
                    filename: 'internal-pdf-viewer',
                    length: 1,
                })),
            });

            // Fake languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });

            // Override permissions query
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters);

            // Chrome runtime
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };

            // WebGL vendor/renderer
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, parameter);
            };
        """)
        logger.info("Browser started on display %s", settings.display_number)

    async def new_page(self) -> Page:
        if self._context is None:
            await self._start()
        return await self._context.new_page()

    async def close(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        BrowserManager._instance = None
        logger.info("Browser closed")

    async def screenshot(self, page: Page, path: str | None = None) -> bytes:
        return await page.screenshot(path=path, full_page=False)
