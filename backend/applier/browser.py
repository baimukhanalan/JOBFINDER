"""Playwright browser manager for the application pre-fill engine.

Headless by default. Supports per-profile `storage_state` (cookies + localStorage)
so a session the user logged into once (e.g. a Workday tenant) can be reused.
The proxy is OFF by default — the configured proxy is unreliable and a dead proxy
breaks every navigation; pre-filling does not need IP rotation.
"""
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, async_playwright

from backend.config import settings

logger = logging.getLogger(__name__)

# UA MUST match the real host platform — a Windows UA on a Mac (navigator.platform
# = "MacIntel") is an obvious automation tell that trips ATS spam filters. This is a
# macOS Chrome UA to match the machine the co-pilot/submit browser runs on.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Removes the headless/automation fingerprints ATS bot-detection looks for. The
# human still clicks Submit; this only makes the automated browser look like the
# real Chrome it is standing in for, so a genuine submit isn't false-flagged.
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'plugins', {
  get: () => [1,2,3,4,5].map(i => ({ name: 'Plugin ' + i, filename: 'p' + i })) });
window.chrome = { runtime: {}, app: { isInstalled: false },
                  loadTimes: function(){}, csi: function(){} };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(p));
}
const _getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
  if (p === 37445) return 'Intel Inc.';            // UNMASKED_VENDOR_WEBGL
  if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
  return _getParam.call(this, p);
};
"""


def _proxy_config() -> dict | None:
    raw = settings.proxy_url
    if not raw:
        return None
    p = urlparse(raw)
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = p.username
        cfg["password"] = p.password or ""
    return cfg


class BrowserManager:
    """Owns a single Playwright browser. Create one per apply run, then close it."""

    def __init__(self, headless: bool = True, use_proxy: bool = False):
        self.headless = headless
        self.use_proxy = use_proxy
        self._pw = None
        self._browser: Browser | None = None
        self._real_chrome = False  # True when the real Google Chrome binary is in use

    async def __aenter__(self) -> "BrowserManager":
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    async def start(self) -> "BrowserManager":
        self._pw = await async_playwright().start()
        # ignore_default_args drops Playwright's --enable-automation (the switch that
        # sets navigator.webdriver and the automation banner). --no-sandbox is itself a
        # bot tell, so it's gone too.
        launch = dict(
            headless=self.headless,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1280,900",
                "--start-maximized",
            ],
        )
        # Prefer the REAL Google Chrome binary over Playwright's bundled Chromium:
        # bundled Chromium has a different TLS/JA3 fingerprint that ATS bot-detection
        # flags before any JS runs. Fall back to Chromium if Chrome isn't installed.
        try:
            self._browser = await self._pw.chromium.launch(channel="chrome", **launch)
            self._real_chrome = True
            logger.info("Browser started (real Chrome, headless=%s)", self.headless)
        except Exception as exc:
            logger.warning("real Chrome unavailable (%s) — using bundled Chromium", exc)
            self._browser = await self._pw.chromium.launch(**launch)
        return self

    async def new_context(self, storage_state: str | None = None) -> BrowserContext:
        kwargs: dict = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
            "timezone_id": "Asia/Almaty",  # match the KZ candidate story
            "color_scheme": "light",
        }
        # With the real Chrome binary, use its AUTHENTIC user-agent — overriding it
        # with a hardcoded version string only re-introduces a mismatch. Only spoof
        # the UA when we fell back to bundled Chromium.
        if not self._real_chrome:
            kwargs["user_agent"] = _UA
        if self.use_proxy:
            proxy = _proxy_config()
            if proxy:
                kwargs["proxy"] = proxy
        if storage_state and Path(storage_state).exists():
            kwargs["storage_state"] = storage_state
            logger.info("Loaded storage_state from %s", storage_state)
        ctx = await self._browser.new_context(**kwargs)
        await ctx.add_init_script(_STEALTH)
        return ctx

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
        logger.info("Browser closed")


async def save_storage_state(context: BrowserContext, path: str) -> None:
    """Persist a logged-in session's cookies/localStorage for later reuse."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=path)
