"""Telegram bot for semi-auto apply workflow.

Features:
- Sends unknown questions with answer buttons
- Saves answers to QuestionBank for reuse
- Sends form-ready notifications with screenshots
- /status — current apply stats
- /next — trigger next job (when in interactive mode)
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import httpx
from backend.config import settings

logger = logging.getLogger(__name__)

BOT_TOKEN = settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Will be set after first /start
_chat_id = settings.telegram_chat_id or "8794099579"

# Pending questions waiting for user answer: {msg_id: {question_text, job_id, options}}
_pending_questions: dict[int, dict] = {}

# Answer queue: questions answered by user via Telegram
_answer_queue: asyncio.Queue = asyncio.Queue()


_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=60)
    return _http_client


async def _api(method: str, **kwargs) -> dict:
    """Call Telegram Bot API."""
    client = await _get_client()
    resp = await client.post(f"{API_URL}/{method}", **kwargs)
    return resp.json()


async def send_message(text: str, reply_markup: dict | None = None, photo: bytes | None = None) -> dict | None:
    """Send a message to the configured chat."""
    if not _chat_id:
        logger.warning("No chat_id configured. Send /start to the bot first.")
        return None

    if photo:
        data = {"chat_id": _chat_id, "caption": text[:1024], "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        files = {"photo": ("screenshot.png", photo, "image/png")}
        return await _api("sendPhoto", data=data, files=files)
    else:
        data = {"chat_id": _chat_id, "text": text[:4096], "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        return await _api("sendMessage", json=data)


async def send_question(question_text: str, job_title: str, company: str,
                        options: list[str] | None = None, job_id: int = 0) -> None:
    """Send an unknown question to user with answer options."""
    text = (
        f"❓ <b>Unknown question</b>\n"
        f"📋 {job_title} @ {company}\n\n"
        f"<b>Q:</b> {question_text[:500]}"
    )

    reply_markup = None
    if options:
        # Inline keyboard with options
        buttons = []
        for opt in options[:8]:  # Max 8 options
            buttons.append([{
                "text": opt[:40],
                "callback_data": json.dumps({"a": opt[:60], "q": question_text[:100]})[:64],
            }])
        # Add "Skip" button
        buttons.append([{"text": "⏭ Skip", "callback_data": "skip"}])
        reply_markup = {"inline_keyboard": buttons}
    else:
        # For free-text questions, ask user to reply
        text += "\n\n💬 <i>Reply to this message with your answer</i>"
        # Add skip button
        reply_markup = {"inline_keyboard": [[{"text": "⏭ Skip", "callback_data": "skip"}]]}

    result = await send_message(text, reply_markup=reply_markup)

    if result and result.get("ok"):
        msg_id = result["result"]["message_id"]
        _pending_questions[msg_id] = {
            "question_text": question_text,
            "job_id": job_id,
            "options": options,
        }


async def send_form_ready(job_id: int, job_title: str, company: str,
                          filled: int, unanswered: list[str],
                          screenshot: bytes | None = None) -> None:
    """Notify user that form is ready for review."""
    text = (
        f"📝 <b>Form ready for review</b>\n"
        f"#{job_id} {job_title} @ {company}\n\n"
        f"✅ Filled: {filled} fields\n"
    )
    if unanswered:
        text += f"❓ Unanswered: {len(unanswered)}\n"
        for q in unanswered[:3]:
            text += f"  • {q[:50]}\n"

    text += (
        f"\n📺 <a href='{settings.novnc_url}'>Open noVNC</a>\n"
        f"👆 Review form, fill missing, solve CAPTCHA, click Submit"
    )

    buttons = [
        [{"text": "✅ Submitted", "callback_data": "submitted"}],
        [{"text": "⏭ Skip this job", "callback_data": "skip_job"}],
        [{"text": "🛑 Stop", "callback_data": "stop"}],
    ]
    reply_markup = {"inline_keyboard": buttons}

    await send_message(text, reply_markup=reply_markup, photo=screenshot)


async def send_status(stats: dict) -> None:
    """Send current apply stats."""
    text = (
        f"📊 <b>Apply Status</b>\n\n"
        f"✅ Submitted: {stats.get('submitted', 0)}\n"
        f"📝 Filled: {stats.get('filled', 0)}\n"
        f"⛔ Expired: {stats.get('expired', 0)}\n"
        f"❌ Errors: {stats.get('error', 0)}\n"
        f"⏭ Skipped: {stats.get('skipped', 0)}\n"
    )
    await send_message(text)


# Callback data for user actions
_user_action_queue: asyncio.Queue = asyncio.Queue()


async def poll_updates():
    """Poll for Telegram updates and handle them."""
    global _chat_id
    offset = 0

    while True:
        try:
            result = await _api("getUpdates", json={"offset": offset, "timeout": 10})
            if not result.get("ok"):
                await asyncio.sleep(5)
                continue

            updates = result.get("result", [])
            if updates:
                logger.info("Got %d updates", len(updates))

            for update in updates:
                offset = update["update_id"] + 1
                logger.info("Update: %s", json.dumps(update, default=str)[:300])

                # Handle /start
                if "message" in update:
                    msg = update["message"]
                    chat_id = str(msg["chat"]["id"])

                    if not _chat_id:
                        _chat_id = chat_id
                        logger.info("Chat ID set: %s", chat_id)
                        await send_message("🤖 Bot activated! I'll send you questions during auto-apply.")

                    text = msg.get("text", "")

                    if text == "/start":
                        await send_message(
                            "🤖 <b>AutoCaptcher Bot</b>\n\n"
                            "I help with semi-automatic job applications.\n\n"
                            "During auto-apply, I will:\n"
                            "• Send unknown questions for you to answer\n"
                            "• Notify when a form is ready for review\n"
                            "• Save your answers for future use\n\n"
                            "Commands:\n"
                            "/status — current stats\n"
                            "/start — activate bot"
                        )
                    elif text == "/status":
                        await _user_action_queue.put({"action": "status"})

                    # Check if reply to a pending question
                    elif msg.get("reply_to_message"):
                        reply_to = msg["reply_to_message"]["message_id"]
                        if reply_to in _pending_questions:
                            q = _pending_questions.pop(reply_to)
                            await _answer_queue.put({
                                "question_text": q["question_text"],
                                "answer_text": text.strip(),
                                "job_id": q["job_id"],
                            })
                            await send_message(f"💾 Answer saved: <b>{text.strip()[:50]}</b>")

                # Handle callback queries (button clicks)
                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_data = cb.get("data", "")
                    cb_id = cb["id"]
                    logger.info("Callback: data=%s id=%s", cb_data, cb_id)

                    # Acknowledge the callback
                    await _api("answerCallbackQuery", json={"callback_query_id": cb_id})

                    if cb_data == "skip":
                        # Question skip — put in answer queue, not action queue
                        await _answer_queue.put({
                            "question_text": "_skipped",
                            "answer_text": "",
                        })
                        await send_message("⏭ Question skipped")

                    elif cb_data == "submitted":
                        logger.info(">>> Putting 'submitted' into action queue")
                        await _user_action_queue.put({"action": "submitted"})
                        await send_message("✅ Moving to next job...")

                    elif cb_data == "skip_job":
                        logger.info(">>> Putting 'skip_job' into action queue")
                        await _user_action_queue.put({"action": "skip_job"})
                        await send_message("⏭ Skipping this job...")

                    elif cb_data == "stop":
                        logger.info(">>> Putting 'stop' into action queue")
                        await _user_action_queue.put({"action": "stop"})
                        await send_message("🛑 Stopping...")

                    else:
                        # Try parsing as answer option
                        try:
                            data = json.loads(cb_data)
                            if "a" in data and "q" in data:
                                await _answer_queue.put({
                                    "question_text": data["q"],
                                    "answer_text": data["a"],
                                })
                                await send_message(f"💾 Answer: <b>{data['a'][:50]}</b>")
                        except (json.JSONDecodeError, KeyError):
                            pass

        except Exception as e:
            logger.error("Poll error: %s: %s", type(e).__name__, e)
            await asyncio.sleep(3)


async def wait_for_user_action(timeout: int = 300) -> str:
    """Wait for user to click a button (submitted/skip/stop). Returns action string."""
    logger.info("Waiting for user action (timeout=%ds)...", timeout)
    try:
        result = await asyncio.wait_for(_user_action_queue.get(), timeout=timeout)
        action = result.get("action", "timeout")
        logger.info("Got user action: %s", action)
        return action
    except asyncio.TimeoutError:
        logger.info("User action timed out after %ds", timeout)
        return "timeout"


async def get_answer(timeout: int = 120) -> dict | None:
    """Get an answer from the queue. Returns {question_text, answer_text} or None."""
    try:
        return await asyncio.wait_for(_answer_queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def get_chat_id() -> str | None:
    return _chat_id


# For standalone testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def main():
        print("Starting bot polling... Send /start to @autocaptcherbot")
        await poll_updates()

    asyncio.run(main())
