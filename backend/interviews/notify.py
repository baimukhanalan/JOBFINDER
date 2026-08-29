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
from backend.interviews import slots
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


_BOT_USERNAME: str | None = None
_OFFSET_FILE = __import__("pathlib").Path(__file__).resolve().parents[2] / "logs" / "iv_tg_offset"


def bot_username() -> str | None:
    """The notifier bot's @username (for t.me deep links). Cached; None if unavailable."""
    global _BOT_USERNAME
    if _BOT_USERNAME is not None:
        return _BOT_USERNAME or None
    token = _bot_token()
    if not token:
        return None
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
        _BOT_USERNAME = (r.get("result") or {}).get("username") or ""
    except Exception as e:
        logger.warning("bot_username: getMe error: %s", type(e).__name__)
        _BOT_USERNAME = ""
    return _BOT_USERNAME or None


def send_document(chat_id: int, file_path, caption: str = "") -> bool:
    """Send a local file (e.g. the tailored résumé PDF) as a Telegram document. Same
    token-safe logging as send_dm; returns False on any problem (never raises)."""
    import os
    token = _bot_token()
    if not token or not chat_id or not file_path or not os.path.exists(file_path):
        return False
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with open(file_path, "rb") as fh:
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption[:1000]
            resp = httpx.post(url, data=data,
                              files={"document": (os.path.basename(file_path), fh, "application/pdf")},
                              timeout=30)
        if resp.status_code != 200:
            logger.warning("send_document: chat %s failed: %s %s",
                           chat_id, resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as e:
        logger.warning("send_document: chat %s transport error: %s", chat_id, type(e).__name__)
        return False


def _read_offset() -> int:
    try:
        return int(_OFFSET_FILE.read_text().strip() or 0)
    except Exception:
        return 0


def _write_offset(n: int) -> None:
    try:
        _OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OFFSET_FILE.write_text(str(n))
    except Exception:
        pass


def poll_updates() -> int:
    """Poll getUpdates and process `/start <code>` deep-link presses: bind the pressing
    user's chat_id to the responsible whose `tg_link_code` matches, and confirm. The
    offset is PERSISTED (`logs/iv_tg_offset`) so a daemon restart never re-processes an
    already-consumed update. Best-effort; never raises. Returns the number of links made."""
    token = _bot_token()
    if not token:
        return 0
    offset = _read_offset()
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                      params={"offset": offset, "timeout": 0}, timeout=15).json()
    except Exception as e:
        logger.warning("poll_updates: getUpdates error: %s", type(e).__name__)
        return 0
    linked = 0
    new_offset = offset
    for upd in (r.get("result") or []):
        new_offset = max(new_offset, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not chat or not text.startswith("/start"):
            continue
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            send_dm(chat, "Откройте кабинет интервьюера и нажмите «Подключить Telegram», "
                          "чтобы получать сюда напоминания о собеседованиях.")
            continue
        row = db.link_telegram_by_code(code, int(chat))
        if row:
            linked += 1
            send_dm(chat, f"✅ Готово, {row.get('name') or ''}! Напоминания о собеседованиях "
                          "будут приходить в этот чат.")
        else:
            send_dm(chat, "Ссылка для подключения устарела. Откройте кабинет и нажмите "
                          "«Подключить Telegram» ещё раз.")
    if new_offset != offset:
        _write_offset(new_offset)
    return linked


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


def target_chat(interview: dict) -> int | None:
    """The chat_id a document/rich message should go to: the responsible's personal
    chat if linked, else the owner/team chat."""
    rid = interview.get("responsible_id")
    resp = db.get_responsible(rid) if rid is not None else None
    return (resp or {}).get("telegram_chat_id") or settings.telegram_chat_id or None


def rich_reminder_text(interview: dict, responsible_name: str, tz: str | None, pack: dict) -> str:
    """The -60 reminder: everything the interviewer needs one hour ahead — company,
    role, which persona is up, the meeting link, and the time in their own zone. The
    résumé PDF is sent separately as a document."""
    lines = ["⏰ Собеседование через 60 минут",
             f"Ответственный: {responsible_name}"]
    if pack.get("persona_name"):
        lines.append(f"Кандидат (профиль): {pack['persona_name']}")
    lines.append(f"Компания: {pack.get('company') or interview.get('company') or '—'}")
    if pack.get("title"):
        lines.append(f"Вакансия: {pack['title']}")
    lines.append(f"Время: {_when(interview, tz)}")
    lines.append(f"Ссылка на созвон: {pack['zoom']}" if pack.get("zoom")
                 else "Ссылка на созвон: не найдена автоматически — см. переписку профиля")
    lines.append(f"Почта профиля: {interview.get('mailbox') or '—'}")
    return "\n".join(lines)


# ---- message builders (neutral Russian, plain text, no stack names) ---------------
def _persona(interview: dict) -> str:
    """The persona's local-part (before @) of the mailbox, for a compact label."""
    mailbox = interview.get("mailbox") or ""
    return mailbox.split("@", 1)[0] if mailbox else "—"


def _when(interview: dict, tz: str | None = None) -> str:
    start_ts = interview.get("start_ts")
    if not start_ts:
        return "время не указано"
    # psycopg2 returns timestamptz in the DB SESSION timezone (the pool doesn't pin
    # UTC), so pin UTC before converting to the responsible's own zone for display.
    if start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    z = tz or slots.DEFAULT_TZ
    return slots.to_local(start_ts, z).strftime("%Y-%m-%d %H:%M") + f" ({slots.tz_label(z)})"


def assigned_text(interview: dict, responsible_name: str, tz: str | None = None) -> str:
    return (
        "📅 Назначено собеседование\n"
        f"Ответственный: {responsible_name}\n"
        f"Кандидат: {_persona(interview)}\n"
        f"Компания: {interview.get('company') or '—'}\n"
        f"Время: {_when(interview, tz)}"
    )


def reminder_text(interview: dict, responsible_name: str, minutes: int,
                  tz: str | None = None) -> str:
    return (
        f"⏰ Напоминание: собеседование через {minutes} мин\n"
        f"Ответственный: {responsible_name}\n"
        f"Кандидат: {_persona(interview)}\n"
        f"Компания: {interview.get('company') or '—'}\n"
        f"Время: {_when(interview, tz)}"
    )
