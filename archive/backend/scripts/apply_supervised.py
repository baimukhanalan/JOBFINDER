"""Supervised apply script — takes screenshots for Claude to analyze.

Usage: Called step by step. Each call processes one job:
  1. navigate(job_id) — opens page, takes screenshot
  2. fill(job_id, fields) — fills fields based on Claude's analysis
  3. submit(job_id) — submits the form
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
os.environ["DISPLAY"] = ":99"

from sqlalchemy import select, update
from backend.applier.browser import BrowserManager
from backend.models.database import async_session
from backend.models.job import Job
from backend.models.apply_models import ApplyQueue, ApplyStatus, UserProfile

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Global page reference
_page = None


async def get_page():
    global _page
    if _page is None or _page.is_closed():
        bm = await BrowserManager.get_instance()
        _page = await bm.new_page()
    return _page


async def navigate(job_id: int) -> str:
    """Navigate to job apply page and take screenshot. Returns screenshot path."""
    async with async_session() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            return f"Job {job_id} not found"

        url = job.apply_url if (job.apply_url and job.apply_url != "NOT_FOUND") else job.url
        logger.info("Job: %s @ %s", job.title, job.company)
        logger.info("URL: %s", url)

    page = await get_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(4000)

    # Try clicking Apply button
    for sel in [
        'button:has-text("Apply")', 'a:has-text("Apply")',
        'button:has-text("I\'m interested")', 'a:has-text("I\'m interested")',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                logger.info("Clicked: %s", sel)
                await page.wait_for_timeout(4000)
                break
        except Exception:
            continue

    path = f"/tmp/apply_{job_id}.png"
    await page.screenshot(path=path, full_page=True)
    logger.info("Screenshot: %s", path)
    return path


async def fill_fields(fields: list[dict]) -> dict:
    """Fill form fields. Each field: {selector, value, action?}"""
    page = await get_page()
    results = {"success": 0, "failed": 0, "errors": []}

    for f in fields:
        sel = f["selector"]
        val = f.get("value", "")
        action = f.get("action", "fill")

        try:
            element = page.locator(sel).first

            # Handle web components
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            if tag not in ("input", "select", "textarea"):
                inner = element.locator("input:not([role='combobox']), textarea, select").first
                try:
                    if await inner.count() > 0 and await inner.is_visible(timeout=1000):
                        element = inner
                except Exception:
                    inner2 = element.locator("input, textarea, select").first
                    if await inner2.count() > 0:
                        element = inner2

            if action == "fill":
                try:
                    await element.fill(val, timeout=5000)
                except Exception:
                    await element.click(timeout=3000)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(val, delay=30)

            elif action == "select":
                await element.select_option(label=val, timeout=5000)

            elif action == "check":
                try:
                    await element.check(timeout=3000)
                except Exception:
                    await element.click(timeout=3000)

            elif action == "upload":
                await element.set_input_files(val, timeout=5000)

            elif action == "click":
                await element.click(timeout=3000)

            results["success"] += 1
            logger.info("OK: %s = %s", sel, str(val)[:40])

        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"{sel}: {e}")
            logger.error("FAIL: %s — %s", sel, str(e)[:80])

    # Take screenshot after filling
    await page.wait_for_timeout(500)
    await page.screenshot(path="/tmp/apply_filled.png", full_page=True)
    return results


async def click_submit(selector: str) -> str:
    """Click submit button and take post-submit screenshot."""
    page = await get_page()
    try:
        btn = page.locator(selector).first
        await btn.click(timeout=5000)
        await page.wait_for_timeout(4000)
        await page.screenshot(path="/tmp/apply_submitted.png", full_page=True)
        return "Submitted. Screenshot: /tmp/apply_submitted.png"
    except Exception as e:
        return f"Submit failed: {e}"


async def scroll_down():
    """Scroll down and take screenshot."""
    page = await get_page()
    await page.evaluate("window.scrollBy(0, 600)")
    await page.wait_for_timeout(500)
    await page.screenshot(path="/tmp/apply_scroll.png", full_page=True)


async def close_modal():
    """Try to close any modal/popup overlay."""
    page = await get_page()
    for sel in [
        'button:has-text("Close")', 'button:has-text("×")',
        'button:has-text("X")', 'button:has-text("Skip")',
        'button:has-text("No thanks")', 'button:has-text("Dismiss")',
        '[aria-label="Close"]', '.modal-close', '.close-button',
        'button:has-text("Accept")', 'button:has-text("I Accept")',
        '#onetrust-accept-btn-handler',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                logger.info("Closed modal: %s", sel)
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


# CLI interface for quick testing
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python apply_supervised.py navigate <job_id>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "navigate":
        job_id = int(sys.argv[2])
        asyncio.run(navigate(job_id))
    elif cmd == "close_modal":
        asyncio.run(close_modal())
