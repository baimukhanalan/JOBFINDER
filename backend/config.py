from pathlib import Path

from pydantic_settings import BaseSettings

ENV_FILE = Path(__file__).parent / ".env"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://programmer:jobfinder123@localhost:5432/jobfinder"
    anthropic_api_key: str = ""
    # Telegram notifications: create a bot via @BotFather -> copy the token to
    # TELEGRAM_BOT_TOKEN.  To find your chat_id: send any message to the bot,
    # then open https://api.telegram.org/bot<TOKEN>/getUpdates and look for
    # "message"."chat"."id".  Set TELEGRAM_CHAT_ID to that value.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    scrape_interval_minutes: int = 30
    proxy_url: str = ""  # e.g. http://user:pass@us-proxy.example.com:8080
    do_api_key: str = ""
    # Local OpenAI-compatible LLM (Sumrak AI on :8080) for résumé tailoring — no external key needed
    llm_url: str = "http://127.0.0.1:8080/v1"
    llm_key: str = "sk-sumrak-ai"
    llm_model: str = "sumrak-smart"
    # Candidate mailboxes.  "mailpit" = local throwaway sink (nothing real arrives);
    # "mailgun" = real inbound mail via a Mailgun catch-all route + stored messages.
    mail_provider: str = "mailpit"
    mailgun_api_key: str = ""
    mailgun_domain: str = ""  # the Mailgun domain that owns the addresses
    mailgun_base: str = "https://api.mailgun.net"
    mail_domain: str = ""  # address domain; defaults to mailgun_domain (or mail.kz on mailpit)
    # Outbound: HTTP API (default) needs only mailgun_api_key; SMTP is an alternative
    # transport using a per-domain SMTP credential from the Mailgun panel.
    mail_send_transport: str = "api"  # "api" | "smtp"
    mailgun_smtp_host: str = "smtp.mailgun.org"
    mailgun_smtp_port: int = 587
    mailgun_smtp_login: str = ""
    mailgun_smtp_password: str = ""

    class Config:
        env_file = str(ENV_FILE)
        extra = "ignore"  # tolerate leftover keys in .env (e.g. archived auto-apply settings)


settings = Settings()
