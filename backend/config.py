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
    # Candidate mailboxes.  "mailpit" = local throwaway sink (nothing real arrives,
    # dev only); "selfhost" = real inbound/outbound mail on OUR OWN Postfix/Dovecot
    # server — no third-party service.
    mail_provider: str = "mailpit"
    mail_domain: str = ""  # address domain (e.g. jobs.systeam.kz); label only on mailpit
    # selfhost inbound: read the Maildir our Postfix delivers into.
    mail_maildir_base: str = "/var/mail/vhosts"
    # selfhost outbound: submit through our own Postfix (127.0.0.1:587, STARTTLS +
    # SASL); OpenDKIM signs on the way out. mail_smtp_login/password is the domain's
    # submission account created on the mail server.
    mail_smtp_host: str = "127.0.0.1"
    mail_smtp_port: int = 587
    mail_smtp_login: str = ""
    mail_smtp_password: str = ""

    class Config:
        env_file = str(ENV_FILE)
        extra = "ignore"  # tolerate leftover keys in .env (e.g. archived auto-apply settings)


settings = Settings()
