import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Errors that mean the proxy itself is unreachable -> fall back to a direct connection
_PROXY_ERRORS = (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout)


@dataclass
class RawJob:
    title: str
    company: str
    url: str
    salary_text: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    location: str = "Remote"
    country: str = "US"
    description: str | None = None
    tags: str | None = None
    source_site: str | None = None


class BaseScraper(ABC):
    source: str

    def __init__(self):
        self._proxy = settings.proxy_url or None
        headers = {"User-Agent": _UA}
        # Primary client: routes through the configured proxy when one is set.
        # Short connect timeout so a dead proxy fails fast and we fall back to direct.
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=8.0), proxy=self._proxy, headers=headers
        )
        # Direct client: fallback used when the proxy is down. Reuses `client` if no proxy.
        self._direct = (
            httpx.AsyncClient(timeout=30, headers=headers) if self._proxy else self.client
        )
        self._proxy_dead = False

    async def fetch(self, url: str, **kwargs) -> httpx.Response:
        """GET `url` through the proxy, transparently falling back to a direct
        connection if the proxy is unreachable. After the first proxy failure the
        proxy is skipped for the rest of this scraper's lifetime."""
        if self._proxy and not self._proxy_dead:
            try:
                return await self.client.get(url, **kwargs)
            except _PROXY_ERRORS as e:
                logger.warning(
                    "%s: proxy unreachable (%s) — falling back to direct connection",
                    getattr(self, "source", "scraper"), type(e).__name__,
                )
                self._proxy_dead = True
        return await self._direct.get(url, **kwargs)

    async def close(self):
        await self.client.aclose()
        if self._direct is not self.client:
            await self._direct.aclose()

    @abstractmethod
    async def scrape(self) -> list[RawJob]:
        pass
