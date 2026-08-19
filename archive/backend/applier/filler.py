import logging

from playwright.async_api import Page

from backend.config import settings

logger = logging.getLogger(__name__)


async def fill_field(page: Page, field: dict) -> bool:
    """Fill a single form field based on analyzer output."""
    selector = field.get("selector", "")
    action = field.get("action", "fill")
    value = field.get("value", "")

    try:
        element = page.locator(selector).first
        if not await element.is_visible(timeout=3000):
            logger.warning("Field not visible: %s", selector)
            return False

        # For Web Components (spl-input, etc.), target the inner <input>
        tag_name = await element.evaluate("el => el.tagName.toLowerCase()")
        if tag_name not in ("input", "select", "textarea") and action in ("fill", "check"):
            # For phone fields, find the actual phone number input (not country dropdown)
            inner = None
            if "phone" in selector.lower() or "tel" in (await element.get_attribute("type") or ""):
                inner_all = await element.locator('input[type="tel"], input[autocomplete="tel"]').all()
                if not inner_all:
                    inner_all = await element.locator("input").all()
                # Pick the visible one that's not the country search
                for inp in inner_all:
                    try:
                        if await inp.is_visible(timeout=500):
                            aria = await inp.get_attribute("aria-label") or ""
                            if "country" not in aria.lower() and "search" not in aria.lower():
                                inner = inp
                                break
                    except Exception:
                        continue
            if inner is None:
                try:
                    candidate = element.locator("input:not([role='combobox']), textarea, select").first
                    if await candidate.count() > 0 and await candidate.is_visible(timeout=1000):
                        inner = candidate
                except Exception:
                    pass
            if inner is None:
                try:
                    candidate = element.locator("input, textarea, select").first
                    if await candidate.count() > 0:
                        inner = candidate
                except Exception:
                    pass
            if inner:
                element = inner

        if action == "fill":
            try:
                await element.clear(timeout=5000)
                await element.fill(value, timeout=5000)
            except Exception:
                # Fallback: click and type
                await element.click(timeout=3000)
                await page.keyboard.press("Control+a")
                await page.keyboard.type(value, delay=50)
            logger.info("Filled '%s' = '%s'", selector, value[:50])

        elif action == "select":
            await element.select_option(label=value)
            logger.info("Selected '%s' = '%s'", selector, value)

        elif action == "check":
            try:
                if value.lower() in ("true", "yes", "1"):
                    await element.check()
                else:
                    await element.uncheck()
            except Exception:
                # Fallback: click the element
                await element.click()
            logger.info("Checked '%s' = '%s'", selector, value)

        elif action == "upload":
            resume = value or settings.resume_path
            if resume:
                await element.set_input_files(resume)
                logger.info("Uploaded resume to '%s'", selector)
            else:
                logger.warning("No resume path configured")
                return False

        elif action == "click":
            await element.click()
            logger.info("Clicked '%s'", selector)

        else:
            logger.warning("Unknown action: %s", action)
            return False

        return True

    except Exception as e:
        logger.error("Failed to fill '%s': %s", selector, e)
        return False


async def fill_form(page: Page, analysis: dict) -> tuple[int, int]:
    """Fill all fields from analysis result. Returns (success_count, fail_count)."""
    success = 0
    fail = 0

    for field in analysis.get("fields", []):
        if await fill_field(page, field):
            success += 1
        else:
            fail += 1
        # Small delay between fields to appear human
        await page.wait_for_timeout(300)

    return success, fail


async def click_submit(page: Page, analysis: dict) -> bool:
    """Click the submit/apply button."""
    selector = analysis.get("submit_selector")
    if not selector:
        logger.warning("No submit selector found")
        return False

    try:
        button = page.locator(selector).first
        if await button.is_visible(timeout=3000):
            await button.click()
            logger.info("Clicked submit: %s", selector)
            await page.wait_for_timeout(2000)
            return True
        else:
            logger.warning("Submit button not visible: %s", selector)
            return False
    except Exception as e:
        logger.error("Failed to click submit: %s", e)
        return False
