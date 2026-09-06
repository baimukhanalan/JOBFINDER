"""One-shot read-only Chromium controller. No answer actions."""
from urllib.parse import urlsplit
from uuid import uuid4
from playwright.async_api import async_playwright
from qa_bot.domain.observation import BrowserSnapshot, PageElement


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only HTTP(S) pages are supported")
    if parsed.username or parsed.password:
        raise ValueError("URL userinfo is not supported")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


class BrowserController:
    def __init__(self, allowed_origins: tuple[str, ...], *, headless: bool = True,
                 launch_args: tuple[str, ...] = (), init_scripts: tuple[str, ...] = (),
                 local_network_origins: tuple[str, ...] = ()):
        if any(not isinstance(value, str) or not value.startswith("--") or len(value) > 4096
               for value in launch_args):
            raise ValueError("invalid Chromium launch argument")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 100_000
               for value in init_scripts):
            raise ValueError("invalid browser init script")
        self.allowed_origins = frozenset(origin(value) for value in allowed_origins)
        normalized_local_network = tuple(origin(value) for value in local_network_origins)
        if any(value not in self.allowed_origins for value in normalized_local_network):
            raise ValueError("local-network permission origin must be allowlisted")
        if any(not value.startswith("https://") for value in normalized_local_network):
            raise ValueError("local-network permission requires an HTTPS page origin")
        self.headless = headless
        self.launch_args = tuple(launch_args)
        self.init_scripts = tuple(init_scripts)
        self.local_network_origins = normalized_local_network
        self._engine = self._browser = self._context = self._page = None
        self.blocked_origins = set()
        self.page_error_count = self.failed_request_count = 0

    def _page_error(self, _error):
        self.page_error_count += 1

    def _request_failed(self, _request):
        self.failed_request_count += 1

    @property
    def is_open(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def _route(self, route):
        try:
            permitted = origin(route.request.url) in self.allowed_origins
        except ValueError:
            permitted = False
        if permitted:
            await route.continue_()
        else:
            try:
                self.blocked_origins.add(origin(route.request.url))
            except ValueError:
                pass
            await route.abort()

    async def open(self, url: str, *, timeout: float = 30) -> None:
        if origin(url) not in self.allowed_origins:
            raise ValueError("Page origin is not allowed")
        if self.is_open:
            raise RuntimeError("Controller already has an open session")
        self.blocked_origins.clear()
        self.page_error_count = self.failed_request_count = 0
        try:
            self._engine = await async_playwright().start()
            self._browser = await self._engine.chromium.launch(
                headless=self.headless, args=list(self.launch_args))
            self._context = await self._browser.new_context(
                service_workers="block", accept_downloads=False)
            for permitted_origin in self.local_network_origins:
                await self._context.grant_permissions(
                    ["local-network-access"], origin=permitted_origin)
            for script in self.init_scripts:
                await self._context.add_init_script(script=script)
            await self._context.route("**/*", self._route)
            self._page = await self._context.new_page()
            self._page.on("pageerror", self._page_error)
            self._page.on("requestfailed", self._request_failed)
            self._page.on("dialog", lambda dialog: dialog.dismiss())
            await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if origin(self._page.url) not in self.allowed_origins:
                raise ValueError("Navigation left allowed origins")
        except BaseException:
            await self.close()
            raise

    async def wait_for_content(self, *, timeout: float = 10, min_characters: int = 1) -> bool:
        """Wait for SPA hydration, without clicks or unbounded networkidle waits."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        if not self.is_open or self._page is None:
            raise RuntimeError("Browser is not open")
        try:
            await self._page.wait_for_function(
                """minimum => {
                    const roots = [document];
                    for (let i=0; i<roots.length; i++) {
                        for (const e of roots[i].querySelectorAll('*')) if(e.shadowRoot) roots.push(e.shadowRoot);
                    }
                    return roots.some(r => (r.body?.innerText || r.textContent || '').trim().length >= minimum);
                }""",
                arg=min_characters, timeout=timeout * 1000)
            return True
        except PlaywrightTimeout:
            return False

    async def observe(self, *, timeout: float = 30) -> BrowserSnapshot:
        if not self.is_open or self._page is None:
            raise RuntimeError("Browser is not open")
        self._page.set_default_timeout(timeout * 1000)
        await self._page.locator("body").wait_for(state="attached")
        data = await self._page.evaluate("""() => {
            const roots = [document];
            for (let i=0; i<roots.length; i++) {
                for (const e of roots[i].querySelectorAll('*')) if(e.shadowRoot) roots.push(e.shadowRoot);
            }
            const all = selector => roots.flatMap(r => Array.from(r.querySelectorAll(selector)));
            const visible = e => {
                const s = getComputedStyle(e);
                return e.getClientRects().length > 0 && s.visibility !== 'hidden'
                    && s.display !== 'none' && !e.closest('[aria-hidden="true"]');
            };
            const text = e => (e.innerText || e.textContent || '').trim();
            const name = e => e.getAttribute('aria-label') ||
                (e.getAttribute('aria-labelledby') || '').split(/\\s+/)
                  .map(id => document.getElementById(id)?.textContent || '').join(' ').trim() ||
                Array.from(e.labels || []).map(text).join(' ') ||
                (e.matches('input') ? e.getAttribute('placeholder') || '' : text(e));
            const elements = all(
                'button,a[href],input:not([type=hidden]),select,textarea,[role],[tabindex]'
            ).filter(visible).map((e,i) => ({
                ref: e.id || 'element-' + i, tag: e.tagName.toLowerCase(),
                role: e.getAttribute('role') || '', input_type: e.getAttribute('type') || '',
                name: name(e), enabled: !e.disabled && e.getAttribute('aria-disabled') !== 'true'
            }));
            const clone = document.body.cloneNode(true);
            const originals = document.body.querySelectorAll('*');
            const copies = clone.querySelectorAll('*');
            originals.forEach((e,i) => { if (!visible(e)) copies[i].setAttribute('hidden',''); });
            const shadowText = roots.slice(1).map(r => {
                const walker = document.createTreeWalker(r, NodeFilter.SHOW_TEXT);
                const parts = []; let n;
                while(n = walker.nextNode()) {
                    if(n.parentElement && visible(n.parentElement) && !n.parentElement.closest('script,style,noscript')) parts.push(n.textContent);
                }
                return parts.join(' ');
            });
            return {text: [document.body.innerText, ...shadowText].join(' ').trim(), html: clone.outerHTML,
                shadowRootCount: roots.length - 1,
                headings: all('h1,h2,[role=heading]')
                    .filter(visible).map(text), elements};
        }""")
        return BrowserSnapshot(
            observation_id=str(uuid4()), document_ref=self._page.url,
            dom_text=data["text"], title=await self._page.title(),
            headings=tuple(data["headings"]),
            elements=tuple(PageElement(**item) for item in data["elements"]),
            incomplete=bool(self._page.frames[1:]),
            question_html=data["html"],
            blocked_origins=tuple(sorted(self.blocked_origins)),
            shadow_root_count=data["shadowRootCount"],
            page_error_count=self.page_error_count,
            failed_request_count=self.failed_request_count,
        )

    async def capture(self, path):
        """Explicit local diagnostic artifact; no URL bar, no tracing/session dump."""
        if not self.is_open or self._page is None:
            raise RuntimeError("Browser is not open")
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(path), full_page=False, animations="disabled")

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            try:
                if self._browser is not None:
                    await self._browser.close()
            finally:
                try:
                    if self._engine is not None:
                        await self._engine.stop()
                finally:
                    self._page = self._context = self._browser = self._engine = None
