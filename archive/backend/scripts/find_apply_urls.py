"""Find direct apply URLs for jobs by scraping company career pages via Google."""
import asyncio
import logging
import re
import sys
import os
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import httpx
from sqlalchemy import select, update

from backend.config import settings
from backend.models.database import async_session
from backend.models.job import Job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# Known ATS domains
ATS_DOMAINS = [
    "greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co",
    "myworkdayjobs.com",
    "bamboohr.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "jobvite.com",
    "icims.com",
    "workable.com",
    "breezy.hr",
    "jazz.co", "applytojob.com",
    "recruitee.com",
    "pinpointhq.com",
    "dover.com",
]

PROXY = settings.proxy_url or None
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


async def search_google(query: str, client: httpx.AsyncClient) -> list[str]:
    """Search Google and return result URLs."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}&num=10&hl=en"

    try:
        resp = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=15)
        if resp.status_code != 200:
            logger.warning("Google returned %d for: %s", resp.status_code, query)
            return []

        # Extract URLs from Google results
        urls = re.findall(r'href="(/url\?q=([^&"]+))', resp.text)
        result_urls = []
        for _, u in urls:
            decoded = urllib.parse.unquote(u)
            if decoded.startswith("http") and "google.com" not in decoded:
                result_urls.append(decoded)
        return result_urls

    except Exception as e:
        logger.error("Google search error: %s", e)
        return []


async def search_duckduckgo(query: str, client: httpx.AsyncClient) -> list[str]:
    """Search DuckDuckGo HTML version and return result URLs."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        resp = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=15)
        if resp.status_code != 200:
            return []

        # Extract result URLs
        urls = re.findall(r'href="(https?://[^"]+)"', resp.text)
        return [u for u in urls if "duckduckgo.com" not in u]

    except Exception as e:
        logger.error("DDG search error: %s", e)
        return []


def extract_apply_url(urls: list[str], company: str) -> str | None:
    """Find the best apply URL from search results."""
    company_lower = company.lower().replace(" ", "").replace(",", "").replace(".", "")

    # Priority 1: ATS links
    for url in urls:
        url_lower = url.lower()
        for ats in ATS_DOMAINS:
            if ats in url_lower:
                return url

    # Priority 2: Company career/jobs pages
    for url in urls:
        url_lower = url.lower()
        if any(kw in url_lower for kw in ["/careers", "/jobs", "/apply", "/openings", "/positions"]):
            # Check it's actually the company's site
            url_domain = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
            if any(part in url_domain for part in company_lower.split() if len(part) > 3):
                return url

    # Priority 3: Any ATS-like URL
    for url in urls:
        url_lower = url.lower()
        if any(kw in url_lower for kw in ["apply", "careers", "jobs", "openings"]):
            if not any(agg in url_lower for agg in ["indeed.com", "linkedin.com", "glassdoor.com", "remoteok.com", "ziprecruiter.com"]):
                return url

    return None


def extract_url_from_description(description: str) -> str | None:
    """Try to find apply URL directly in job description."""
    if not description:
        return None

    urls = re.findall(r'https?://[^\s"<>\)]+', description)
    for url in urls:
        url_lower = url.lower()
        for ats in ATS_DOMAINS:
            if ats in url_lower:
                return url.rstrip(".,;)")
        if any(kw in url_lower for kw in ["/careers/", "/jobs/", "/apply/", "/openings/"]):
            if not any(agg in url_lower for agg in ["indeed.com", "linkedin.com", "glassdoor.com"]):
                return url.rstrip(".,;)")
    return None


async def process_all():
    """Process all jobs without apply_url."""
    async with async_session() as session:
        result = await session.execute(
            select(Job.id, Job.title, Job.company, Job.description)
            .where(Job.apply_url.is_(None))
            .order_by(Job.id)
        )
        jobs = [{"id": r[0], "title": r[1], "company": r[2], "description": r[3]} for r in result.all()]

    total = len(jobs)
    logger.info("Found %d jobs without apply_url", total)

    # Step 1: Extract URLs from descriptions
    found_desc = 0
    async with async_session() as session:
        for job in jobs[:]:
            url = extract_url_from_description(job["description"])
            if url:
                await session.execute(
                    update(Job).where(Job.id == job["id"]).values(apply_url=url)
                )
                found_desc += 1
                jobs.remove(job)
                logger.info("  [desc] %s @ %s -> %s", job["title"][:40], job["company"], url[:80])
        await session.commit()

    logger.info("Found %d URLs from descriptions, %d remaining", found_desc, len(jobs))

    # Step 2: Search for the rest
    found_search = 0
    not_found = 0

    proxy_cfg = PROXY if PROXY else None
    async with httpx.AsyncClient(proxy=proxy_cfg) as client:
        for i, job in enumerate(jobs):
            query = f'{job["company"]} careers {job["title"]} apply'
            logger.info("[%d/%d] Searching: %s", i + 1, len(jobs), query[:60])

            # Try DuckDuckGo first (less likely to block)
            urls = await search_duckduckgo(query, client)
            if not urls:
                urls = await search_google(query, client)

            apply_url = extract_apply_url(urls, job["company"])

            async with async_session() as session:
                if apply_url:
                    await session.execute(
                        update(Job).where(Job.id == job["id"]).values(apply_url=apply_url)
                    )
                    found_search += 1
                    logger.info("  -> %s", apply_url[:80])
                else:
                    await session.execute(
                        update(Job).where(Job.id == job["id"]).values(apply_url="NOT_FOUND")
                    )
                    not_found += 1
                    logger.info("  -> NOT FOUND")
                await session.commit()

            # Rate limit to avoid being blocked
            await asyncio.sleep(3)

            if (i + 1) % 50 == 0:
                logger.info("Progress: %d/%d | from desc: %d | from search: %d | not found: %d",
                            i + 1, len(jobs), found_desc, found_search, not_found)

    logger.info("DONE! From descriptions: %d | From search: %d | Not found: %d | Total: %d",
                found_desc, found_search, not_found, total)


if __name__ == "__main__":
    asyncio.run(process_all())
