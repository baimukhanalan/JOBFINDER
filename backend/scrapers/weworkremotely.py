import logging

from bs4 import BeautifulSoup

from backend.scrapers.base import BaseScraper, RawJob

logger = logging.getLogger(__name__)

# WeWorkRemotely's HTML pages are behind a Cloudflare anti-bot wall (HTTP 403
# "Just a moment..."). Its RSS feeds are NOT gated and return the same listings.
FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
]


class WeWorkRemotelyScraper(BaseScraper):
    source = "weworkremotely"

    async def scrape(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()

        for feed in FEEDS:
            try:
                resp = await self.fetch(feed)
                if resp.status_code != 200:
                    logger.warning("WeWorkRemotely %s -> HTTP %s", feed, resp.status_code)
                    continue
            except Exception as e:
                logger.warning("WeWorkRemotely %s failed: %s", feed, type(e).__name__)
                continue

            soup = BeautifulSoup(resp.text, "xml")
            for item in soup.find_all("item"):
                guid = item.find("guid")
                link = item.find("link")
                url = ((guid.text if guid else "") or (link.text if link else "")).strip()
                if not url or url in seen:
                    continue

                title_el = item.find("title")
                raw_title = (title_el.text if title_el else "").strip()
                if not raw_title:
                    continue
                # WWR RSS titles are "Company: Job Title"
                if ":" in raw_title:
                    company, _, title = raw_title.partition(":")
                    company, title = company.strip(), title.strip()
                else:
                    company, title = "", raw_title

                region_el = item.find("region")
                region = (region_el.text if region_el else "").strip() or "Remote"
                region_lower = region.lower()
                if not self._is_relevant_region(region_lower):
                    continue

                seen.add(url)
                jobs.append(
                    RawJob(
                        title=title,
                        company=company,
                        url=url,
                        location=region,
                        country=self._detect_country(region_lower),
                    )
                )

        return jobs

    def _is_relevant_region(self, region: str) -> bool:
        relevant = [
            "usa", "us", "united states", "canada", "north america",
            "anywhere", "worldwide", "global", "remote",
        ]
        return any(term in region for term in relevant) or region == ""

    def _detect_country(self, region: str) -> str:
        if "canada" in region:
            return "CA"
        return "US"
