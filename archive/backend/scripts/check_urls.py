"""Check all apply URLs and mark dead ones.

Sends HEAD/GET requests and marks 404/expired jobs so they don't show in the queue.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import httpx
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
    "this position has been removed", "expired", "does not exist",
    "the page you were looking for doesn't exist",
    "this job is no longer available",
]

DEAD_STATUS_CODES = {404, 410, 403}


async def check_url(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Check if URL is alive. Returns (status, reason)."""
    try:
        resp = await client.get(url, follow_redirects=True, timeout=15)

        if resp.status_code in DEAD_STATUS_CODES:
            return "dead", f"HTTP {resp.status_code}"

        if resp.status_code == 200:
            body = resp.text.lower()
            for phrase in EXPIRED_PHRASES:
                if phrase in body:
                    return "expired", phrase
            return "alive", "OK"

        return "alive", f"HTTP {resp.status_code}"

    except httpx.TimeoutException:
        return "timeout", "timeout"
    except Exception as e:
        return "error", str(e)[:100]


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--start-from", type=int, default=0)
    args = parser.parse_args()

    # Ensure table exists
    async with async_session() as session:
        await session.execute(text("""
            CREATE TABLE IF NOT EXISTS apply_results (
                id SERIAL PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id),
                status VARCHAR(50) NOT NULL,
                error TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        await session.commit()

    # Get unchecked jobs
    async with async_session() as session:
        result = await session.execute(text("""
            SELECT j.id, j.apply_url
            FROM jobs j
            WHERE j.apply_url IS NOT NULL
              AND j.apply_url != 'NOT_FOUND'
              AND j.id > :start_from
              AND j.id NOT IN (SELECT job_id FROM apply_results)
            ORDER BY j.id
        """), {"start_from": args.start_from})
        jobs = [(r[0], r[1]) for r in result]

    logger.info("Checking %d URLs...", len(jobs))

    alive_count = 0
    dead_count = 0

    proxy = settings.apply_proxy or None
    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None

    async with httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"},
        follow_redirects=True,
        timeout=20,
    ) as client:
        # Process in batches
        for i in range(0, len(jobs), args.batch_size):
            batch = jobs[i:i + args.batch_size]
            tasks = [check_url(client, url) for _, url in batch]
            results = await asyncio.gather(*tasks)

            async with async_session() as session:
                for (job_id, url), (status, reason) in zip(batch, results):
                    if status in ("dead", "expired"):
                        await session.execute(
                            text("INSERT INTO apply_results (job_id, status, error) VALUES (:jid, 'expired', :err)"),
                            {"jid": job_id, "err": reason},
                        )
                        dead_count += 1
                        logger.info("  DEAD #%d: %s (%s)", job_id, reason, url[:60])
                    else:
                        alive_count += 1

                await session.commit()

            logger.info("[%d/%d] alive=%d dead=%d", min(i + args.batch_size, len(jobs)),
                       len(jobs), alive_count, dead_count)

    logger.info("\nDone: %d alive, %d dead/expired", alive_count, dead_count)


if __name__ == "__main__":
    asyncio.run(main())
