"""Find direct job URLs for Indeed jobs using Brave Search.

For each Indeed job, searches for the exact position on the company's
direct career page (Greenhouse, Lever, Workday, etc.).
"""
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import httpx
from sqlalchemy import text
from backend.models.database import async_session

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SKIP_DOMAINS = [
    'indeed.com', 'linkedin.com', 'glassdoor.com', 'ziprecruiter.com',
    'simplyhired.com', 'monster.com', 'careerbuilder.com',
    'facebook.com', 'twitter.com', 'youtube.com', 'wikipedia.org',
    'reddit.com', 'quora.com', 'salary.com', 'flexjobs.com',
    'zippia.com', 'theladders.com', 'builtin.com', 'search.brave.com',
]

# Patterns for direct job URLs (with job IDs — specific positions)
JOB_PATTERNS = [
    r'href="(https?://(?:boards|job-boards)\.greenhouse\.io/[a-zA-Z0-9_-]+/jobs/\d+[^"]*)"',
    r'href="(https?://jobs\.lever\.co/[a-zA-Z0-9._-]+/[a-f0-9-]{36}[^"]*)"',
    r'href="(https?://jobs\.ashbyhq\.com/[a-zA-Z0-9._-]+/[a-f0-9-]{36}[^"]*)"',
    r'href="(https?://[a-z0-9-]+\.wd[0-9]+\.myworkdayjobs\.com/[^"]+)"',
    r'href="(https?://[a-z0-9-]+\.icims\.com/jobs/\d+[^"]*)"',
    r'href="(https?://[a-z0-9-]+\.applytojob\.com/apply/[a-zA-Z0-9]+[^"]*)"',
    r'href="(https?://apply\.workable\.com/[a-z0-9-]+/j/[A-Z0-9]+[^"]*)"',
    r'href="(https?://[a-z0-9-]+\.breezy\.hr/p/[a-z0-9-]+[^"]*)"',
    r'href="(https?://[a-z0-9-]+\.jobvite\.com/[^"]+)"',
    r'href="(https?://careers\.smartrecruiters\.com/[A-Za-z0-9_-]+/\d+[^"]*)"',
]

# Fallback: career page patterns (no specific job ID)
CAREER_PATTERNS = [
    r'href="(https?://(?:careers|jobs)\.[a-z0-9.-]+\.[a-z]{2,}[^"]*)"',
    r'href="(https?://[a-z0-9.-]+\.(?:com|org|net|gov|edu)/(?:careers|jobs|employment|work-with-us|join-us)[^"]*)"',
]


async def search_job(client: httpx.AsyncClient, company: str, title: str) -> str | None:
    """Search Brave for a specific job posting."""
    # Clean title for search
    clean_title = re.sub(r'\s*[-–—|/]\s*Remote.*$', '', title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s*\(.*?\)\s*$', '', clean_title).strip()
    query = f'{company} "{clean_title}" apply'

    try:
        resp = await client.get(
            'https://search.brave.com/search',
            params={'q': query, 'source': 'web'},
            timeout=12,
        )
        if resp.status_code == 429:
            return "RATE_LIMITED"
        if resp.status_code != 200:
            return None

        body = resp.text

        # 1. Try specific job URL patterns (best — has job ID)
        for pattern in JOB_PATTERNS:
            matches = re.findall(pattern, body)
            for m in matches:
                if not any(skip in m for skip in SKIP_DOMAINS):
                    return m

        # 2. Try career page patterns (fallback)
        for pattern in CAREER_PATTERNS:
            matches = re.findall(pattern, body)
            for m in matches:
                if not any(skip in m for skip in SKIP_DOMAINS):
                    return m

    except Exception as e:
        logger.debug("Search error: %s", e)
    return None


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()

    async with async_session() as s:
        q = """
            SELECT id, company, title, apply_url
            FROM jobs
            WHERE apply_url LIKE '%indeed.com%'
            AND id NOT IN (SELECT job_id FROM apply_results)
            ORDER BY id
        """
        if args.offset > 0:
            q += f" OFFSET {args.offset}"
        if args.limit > 0:
            q += f" LIMIT {args.limit}"
        result = await s.execute(text(q))
        jobs = [(r[0], r[1], r[2], r[3]) for r in result]

    logger.info("Searching direct URLs for %d jobs...\n", len(jobs))

    found = 0
    updates = []
    rate_limited = False

    async with httpx.AsyncClient(
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        follow_redirects=True,
    ) as client:
        for i in range(0, len(jobs), args.batch_size):
            if rate_limited:
                logger.info("Rate limited! Stopping at %d/%d", i, len(jobs))
                break

            batch = jobs[i:i + args.batch_size]
            tasks = [search_job(client, company, title) for jid, company, title, _ in batch]
            results = await asyncio.gather(*tasks)

            for (jid, company, title, old_url), new_url in zip(batch, results):
                if new_url == "RATE_LIMITED":
                    rate_limited = True
                    break
                if new_url:
                    found += 1
                    updates.append((jid, new_url))
                    logger.info("  ✓ #%d %s | %s → %s", jid, company[:20], title[:30], new_url[:70])

            done = min(i + args.batch_size, len(jobs))
            if done % 30 == 0 or done == len(jobs):
                logger.info("[%d/%d] found=%d", done, len(jobs), found)

            await asyncio.sleep(args.delay)

    logger.info("\n=== Results ===")
    logger.info("Found: %d / %d jobs", found, len(jobs))

    if not updates:
        return

    async with async_session() as s:
        for job_id, new_url in updates:
            await s.execute(text("UPDATE jobs SET apply_url = :url WHERE id = :id"),
                          {"id": job_id, "url": new_url})
        await s.commit()

    logger.info("Updated %d jobs", len(updates))

    # Stats
    async with async_session() as s:
        r = await s.execute(text("""
            SELECT
                CASE WHEN apply_url LIKE '%indeed.com%' THEN 'indeed'
                     WHEN apply_url LIKE '%linkedin.com%' THEN 'linkedin'
                     ELSE 'direct' END, COUNT(*)
            FROM jobs WHERE apply_url IS NOT NULL AND apply_url != 'NOT_FOUND'
            AND id NOT IN (SELECT job_id FROM apply_results)
            GROUP BY 1 ORDER BY 2 DESC
        """))
        logger.info("\nQueue:")
        for t, c in r.fetchall():
            logger.info("  %5d | %s", c, t)


if __name__ == "__main__":
    asyncio.run(main())
