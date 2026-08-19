"""Resolve your personal Telegram chat_id and write it into backend/.env.

The bot cannot message itself, so TELEGRAM_CHAT_ID must be YOUR chat id. Steps:
  1. Open Telegram, find the bot (@job_findersbot), press Start / send any text.
  2. Run:  python -m backend.tools.tg_resolve
It reads getUpdates, takes the most recent private chat, and updates .env.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx

from backend.config import settings

ENV = Path(__file__).resolve().parents[1] / ".env"


def main() -> None:
    token = settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty in backend/.env")
    r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    updates = r.json().get("result", [])
    chats: list[tuple[int, str]] = []
    for u in updates:
        m = u.get("message") or u.get("edited_message") or {}
        ch = m.get("chat", {})
        if ch.get("type") == "private" and ch.get("id"):
            label = f"{ch.get('first_name','')} @{ch.get('username','')}".strip()
            chats.append((ch["id"], label))
    if not chats:
        raise SystemExit(
            "No private messages yet. Open Telegram, send /start to @job_findersbot, "
            "then re-run this.")
    chat_id, label = chats[-1]
    text = ENV.read_text(encoding="utf-8")
    if re.search(r"^TELEGRAM_CHAT_ID=", text, flags=re.MULTILINE):
        text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={chat_id}",
                      text, flags=re.MULTILINE)
    else:
        text += f"\nTELEGRAM_CHAT_ID={chat_id}\n"
    ENV.write_text(text, encoding="utf-8")
    print(f"Resolved chat_id={chat_id} ({label}) -> written to backend/.env")

    # send a confirmation so you can see it works
    httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
               data={"chat_id": chat_id,
                     "text": "✅ JobFinder connected — you'll get prefill summaries here."},
               timeout=20)


if __name__ == "__main__":
    main()
