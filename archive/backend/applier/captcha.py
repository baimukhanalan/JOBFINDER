import asyncio
import logging

from playwright.async_api import Page

from backend.config import settings

logger = logging.getLogger(__name__)

# Telegram notification function will be injected
_notify_func = None


def set_notify_func(func):
    global _notify_func
    _notify_func = func


CAPTCHA_INDICATORS = [
    "recaptcha",
    "hcaptcha",
    "verify you are human",
    "i'm not a robot",
    "press & hold",
    "verify you are a human",
    "just a moment",
    "checking your browser",
]


async def detect_captcha(page: Page) -> bool:
    """Check if the current page has a CAPTCHA."""
    try:
        content = await page.content()
        content_lower = content.lower()
        for indicator in CAPTCHA_INDICATORS:
            if indicator in content_lower:
                logger.info("CAPTCHA detected: '%s'", indicator)
                return True

        # Check for iframes (reCAPTCHA/hCAPTCHA load in iframes)
        frames = page.frames
        for frame in frames:
            url = frame.url.lower()
            if any(x in url for x in ["recaptcha", "hcaptcha", "challenges.cloudflare"]):
                logger.info("CAPTCHA iframe detected: %s", url)
                return True

    except Exception as e:
        logger.error("CAPTCHA detection error: %s", e)

    return False


async def wait_for_captcha_resolution(page: Page, timeout: int | None = None) -> bool:
    """Send CAPTCHA alert and wait for user to solve it via noVNC.

    Returns True if CAPTCHA resolved, False if timeout.
    """
    timeout = timeout or settings.captcha_timeout_seconds

    # Take screenshot and notify
    screenshot = await page.screenshot()

    if _notify_func:
        await _notify_func(
            f"🔒 CAPTCHA detected!\n"
            f"Solve it here: {settings.novnc_url}\n"
            f"Timeout: {timeout}s",
            photo=screenshot,
        )

    logger.info("Waiting for CAPTCHA resolution (timeout: %ds)...", timeout)

    # Poll every 3 seconds to check if CAPTCHA is gone
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(3)
        elapsed += 3

        if not await detect_captcha(page):
            logger.info("CAPTCHA resolved after %ds", elapsed)
            if _notify_func:
                await _notify_func("✅ CAPTCHA resolved! Continuing...")
            return True

    logger.warning("CAPTCHA timeout after %ds", timeout)
    if _notify_func:
        await _notify_func("⏰ CAPTCHA timeout. Skipping this job.")
    return False
