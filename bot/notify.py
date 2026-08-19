import logging
import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


def notify(text: str, parse_mode: str | None = None) -> bool:
    """Synchronous Telegram notification. parse_mode=None keeps the old
    plain-text behavior; pass "HTML" for formatted digests.

    Returns True on success, False when token/chat_id is unset or on any error.
    Safe to call from cron scripts and non-async contexts.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {"chat_id": settings.telegram_chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = httpx.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("notify: send failed: %s", e)
        return False


async def send_telegram_notification(message: str):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": settings.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML",
        })


def format_job_alert(title: str, company: str, salary_text: str | None, url: str, country: str) -> str:
    salary = f"\n💰 {salary_text}" if salary_text else ""
    return (
        f"🆕 <b>New Job</b>\n\n"
        f"<b>{title}</b>\n"
        f"🏢 {company}\n"
        f"📍 {country} Remote"
        f"{salary}\n\n"
        f"🔗 <a href=\"{url}\">Apply</a>"
    )
