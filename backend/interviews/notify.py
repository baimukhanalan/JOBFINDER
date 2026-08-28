"""Telegram delivery for the interview-scheduler notifier daemon.

Notifier ONLY — this module just posts plain-text Telegram messages (no interactive
bot, no aiogram). Delivery target per interview is the responsible's own
`telegram_chat_id` if set, else the owner/team chat `settings.telegram_chat_id`; a
failed personal send falls back to the owner chat. The httpx call shape mirrors
`bot/notify.py::notify`.
"""
from __future__ import annotations

import logging

import httpx

from backend.config import settings
from backend.interviews import db

logger = logging.getLogger(__name__)


def send_dm(chat_id: int, text: str) -> bool:
    """POST a plain-text message to one chat_id. Returns True on success, False when
    the bot token is unset or on any error (never raises)."""
    if not settings.telegram_bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        r = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("send_dm: send to %s failed: %s", chat_id, e)
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
    return start_ts.strftime("%Y-%m-%d %H:%M") + " GMT"


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
