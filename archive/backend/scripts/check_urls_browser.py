"""Check all apply URLs using real browser (Playwright).

Opens each URL and checks if it's a real job posting or dead/expired.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ["DISPLAY"] = ":99"

from playwright.async_api import async_playwright
from sqlalchemy import text
from backend.models.database import async_session
from backend.config import settings

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

EXPIRED_PHRASES = [
    "no longer available", "position has been filled", "no longer accepting",
    "this job has been closed", "this position is closed",
    "job is no longer", "no longer open", "page not found",
    "this posting has been closed", "job not found",
    "this position has been removed", "does not exist",
    "the page you were looking for", "this job has expired",
    "this job is no longer available", "404", "we couldn't find",
    "this page isn't available", "job has been removed",
    "this role is no longer", "opportunity is no longer",
    "posting has expired", "job may have been filled",
    "this listing has expired", "no results found",
    "this job has already been filled",
]

NO_FORM_INDICATORS = [
    # Generic career/listing pages
    "search results", "job listings", "open positions",
    "browse jobs", "find jobs", "explore careers",
]


async def check_page(page, url: str, timeout: int = 15000) -> tuple[str, str]:
    """Navigate to URL and check if it's a valid job posting."""
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

        if resp and resp.status in (404, 410, 403):
            return "dead", f"HTTP {resp.status}"

        await page.wait_for_timeout(2000)

        body = (await page.inner_text("body")).lower()

        # Check expired
        for phrase in EXPIRED_PHRASES:
            if phrase in body:
                return "expired", phrase

        # Check if it's a real job page (has apply button or form)
        has_apply = await page.locator(
            'button:has-text("Apply"), a:has-text("Apply"), '
            'button:has-text("Submit"), input[type="file"], '
            'button:has-text("Easy Apply"), button:has-text("I\'m interested")'
        ).count()

        if has_apply > 0:
            return "alive", "has_apply_button"

        # Check for form fields
        has_form = await page.locator("form input, form select, form textarea").count()
        if has_form > 2:
            return "alive", "has_form"

        # Could be a job description page (Indeed/LinkedIn) — still valid
        has_job_info = any(x in body for x in ["job description", "qualifications", "responsibilities", "requirements", "salary", "benefits"])
        if has_job_info:
            return "alive", "job_description_page"

        # Very short page — probably error
        if len(body) < 200:
            return "dead", "empty_page"

        return "alive", "unclear"

    except Exception as e:
        err = str(e)[:80]
        if "timeout" in err.lower() or "Timeout" in err:
            return "timeout", "page_load_timeout"
        return "error", err


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # Ensure table
    async with async_session() as s:
        await s.execute(text("""
            CREATE TABLE IF NOT EXISTS apply_results (
                id SERIAL PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id),
                status VARCHAR(50) NOT NULL,
                error TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        await s.commit()

    # Get unchecked jobs
    async with async_session() as s:
        q = """
            SELECT j.id, j.apply_url, j.source
            FROM jobs j
            WHERE j.apply_url IS NOT NULL AND j.apply_url != 'NOT_FOUND'
            AND j.id NOT IN (SELECT job_id FROM apply_results)
            ORDER BY j.id
        """
        if args.limit > 0:
            q += f" LIMIT {args.limit}"
        result = await s.execute(text(q))
        jobs = [(r[0], r[1], r[2]) for r in result]

    logger.info("Checking %d URLs with browser...", len(jobs))

    pw = await async_playwright().start()

    proxy = None
    if settings.apply_proxy:
        from urllib.parse import urlparse
        parsed = urlparse(settings.apply_proxy)
        proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy["username"] = parsed.username
            proxy["password"] = parsed.password or ""

    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        proxy=proxy,
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )

    alive = 0
    dead = 0
    total = len(jobs)

    # Process in batches with multiple pages
    for i in range(0, total, args.batch_size):
        batch = jobs[i:i + args.batch_size]
        pages = []
        results = []

        # Open pages in parallel
        for jid, url, source in batch:
            page = await context.new_page()
            pages.append(page)

        tasks = []
        for idx, (jid, url, source) in enumerate(batch):
            tasks.append(check_page(pages[idx], url))

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        async with async_session() as s:
            for idx, (jid, url, source) in enumerate(batch):
                r = batch_results[idx]
                if isinstance(r, Exception):
                    status, reason = "error", str(r)[:80]
                else:
                    status, reason = r

                if status in ("dead", "expired"):
                    await s.execute(
                        text("INSERT INTO apply_results (job_id, status, error) VALUES (:j, 'expired', :e)"),
                        {"j": jid, "e": reason},
                    )
                    dead += 1
                    logger.info("  DEAD #%d [%s]: %s", jid, source, reason)
                elif status == "timeout":
                    # Don't mark as dead — might just be slow
                    alive += 1
                else:
                    alive += 1

            await s.commit()

        # Close pages
        for page in pages:
            try:
                await page.close()
            except:
                pass

        done = min(i + args.batch_size, total)
        logger.info("[%d/%d] alive=%d dead=%d (%.0f%%)", done, total, alive, dead,
                    done / total * 100)

    await context.close()
    await browser.close()
    await pw.stop()

    logger.info("\nDone: %d alive, %d dead/expired out of %d", alive, dead, total)

    # Final stats
    async with async_session() as s:
        r = await s.execute(text("""
            SELECT COUNT(*) FROM jobs j
            WHERE j.apply_url IS NOT NULL AND j.apply_url != 'NOT_FOUND'
            AND j.id NOT IN (SELECT job_id FROM apply_results)
        """))
        logger.info("Final queue: %d jobs", r.scalar())


if __name__ == "__main__":
    asyncio.run(main())
