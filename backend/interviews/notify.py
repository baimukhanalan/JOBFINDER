"""Telegram delivery for the interview-scheduler notifier daemon.

Notifier ONLY — this module just posts plain-text Telegram messages (no interactive
bot, no aiogram). Delivery target per interview is the responsible's own
`telegram_chat_id` if set, else the owner/team chat `settings.telegram_chat_id`; a
failed personal send falls back to the owner chat. The httpx call shape mirrors
`bot/notify.py::notify`.
"""
from __future__ import annotations

import logging
from datetime import timezone

import httpx

from backend.config import settings
from backend.interviews import db

logger = logging.getLogger(__name__)

# httpx logs the full request URL at INFO, and that URL embeds the bot token
# (`.../bot<TOKEN>/sendMessage`). The daemon runs with basicConfig(INFO), so without
# this the token would leak into the pm2 log — defeating send_dm's own no-URL logging.
# Pin httpx to WARNING at import so ANY importer (daemon, tests, ad-hoc) is protected.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _bot_token() -> str:
    """The interview notifier's Telegram token: its dedicated IV_BOT_TOKEN if set,
    else the project-wide telegram_bot_token as a fallback. Keeping a separate token
    isolates the notifier from the main project bot."""
    return settings.iv_bot_token or settings.telegram_bot_token


def send_dm(chat_id: int, text: str) -> bool:
    """POST a plain-text message to one chat_id. Returns True on success, False when
    the bot token is unset or on any error (never raises).

    SECURITY: never log the request URL or the raw exception — the URL embeds the bot
    token (`.../bot<TOKEN>/sendMessage`) and `httpx.HTTPStatusError.__str__()` includes
    it. We inspect the response and log only `status_code` + Telegram's error JSON
    (which carries no token), and on a transport error log only the exception TYPE.
    """
    token = _bot_token()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        if resp.status_code != 200:
            logger.warning("send_dm: chat %s failed: %s %s",
                           chat_id, resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as e:
        logger.warning("send_dm: chat %s transport error: %s", chat_id, type(e).__name__)
        return False


def notify_responsible(interview: dict, text: str) -> bool:
    """Send `text` to the interview's responsible: their personal chat if set, else the
    owner chat. If the personal send fails, fall back to the owner chat. Returns whether
    any send succeeded."""
    rid = interview.get("responsible_id")
    resp = db.get_responsible(rid) if rid is not None else None
    personal = (resp or {}).get("telegram_chat_id")
    owner = settings.telegram_chat_id

    chat = personal or owner
    if not chat:
        return False

    if send_dm(chat, text):
        return True
    # personal chat failed -> fall back to the owner chat (once)
    if personal and owner and str(owner) != str(personal):
        return send_dm(owner, text)
    return False


# ---- message builders (neutral Russian, plain text, no stack names) ---------------
def _persona(interview: dict) -> str:
    """The persona's local-part (before @) of the mailbox, for a compact label."""
    mailbox = interview.get("mailbox") or ""
    return mailbox.split("@", 1)[0] if mailbox else "—"


def _when(interview: dict) -> str:
    start_ts = interview.get("start_ts")
    if not start_ts:
        return "время не указано"
    # psycopg2 returns timestamptz in the DB SESSION timezone (the pool doesn't pin
    # UTC), so convert to UTC before formatting or the hour won't match the "GMT" label.
    if start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    return start_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " GMT"


def assigned_text(interview: dict, responsible_name: str) -> str:
    return (
        "📅 Назначено собеседование\n"
        f"Ответственный: {responsible_name}\n"
        f"Кандидат: {_persona(interview)}\n"
        f"Компания: {interview.get('company') or '—'}\n"
        f"Время: {_when(interview)}"
    )


def reminder_text(interview: dict, responsible_name: str, minutes: int) -> str:
    return (
        f"⏰ Напоминание: собеседование через {minutes} мин\n"
        f"Ответственный: {responsible_name}\n"
        f"Кандидат: {_persona(interview)}\n"
        f"Компания: {interview.get('company') or '—'}\n"
        f"Время: {_when(interview)}"
    )
