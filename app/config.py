"""
Centralized settings, loaded from environment variables (.env in local dev).

Security note: every credential here (WhatsApp token, Meta app secret, Anthropic
and Deepgram keys) is read from the environment ONLY. Nothing is hardcoded. In
production these should come from your platform's secret manager (Render/Railway
secrets, AWS Secrets Manager, etc.) rather than a committed .env file — .env is
for local development only and is gitignored in this project.
"""
from functools import lru_cache
# pyrefly: ignore [missing-import]
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    APP_NAME: str = "OrderMate"
    ENVIRONMENT: str = "development"  # development | production
    LOG_LEVEL: str = "INFO"

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./ordermate.db"

    # --- WhatsApp Cloud API (Meta) ---
    # Leave these blank in local dev -> the app runs in DRY_RUN mode automatically
    # (outgoing messages are logged instead of sent). Fill them in once you have a
    # verified WhatsApp Business number. See README.md for the full setup guide.
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_APP_SECRET: str = ""          # used to verify incoming webhook signatures
    WHATSAPP_VERIFY_TOKEN: str = "ordermate_verify_me"  # you choose this; must match Meta dashboard

    # --- Anthropic (optional in dev) ---
    # Without this, the app falls back to a simple rule-based intent/order parser
    # (see app/ai.py) so you can develop and demo the full flow with zero API cost.
    # Defaults to Haiku rather than a flagship model — this bot's tasks (intent
    # classification, structured field extraction) are lightweight and don't need
    # a larger model's cost.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"

    # --- Deepgram (optional in dev) ---
    # Powers voice note transcription (see app/ai.py::transcribe_voice_note).
    # No rule-based fallback exists for audio, so without this, voice notes get
    # a "please type instead" reply rather than being understood.
    DEEPGRAM_API_KEY: str = ""

    # --- Business owner alerting ---
    OWNER_WHATSAPP_NUMBER: str = ""  # where low-rating alerts, human-handoff requests, and the weekly digest get sent

    # --- Feedback timing ---
    # 3 hours in production; override to a few seconds in dev/tests so you don't
    # have to wait to see the full loop run.
    FEEDBACK_DELAY_SECONDS: int = 60 * 60 * 3

    # --- Abandoned order recovery ---
    # How long a customer can go quiet mid-order before we nudge them. Every reply
    # while still ordering reschedules this (see app/scheduler.py), so it only ever
    # fires once per actual silence, not once per turn.
    ABANDONED_ORDER_NUDGE_SECONDS: int = 60 * 15

    # --- Typing indicator ---
    # How long to show the "typing..." bubble before actually replying — purely
    # cosmetic, makes the bot feel less instant/robotic. 0 disables the pause
    # (indicator still fires, just with no deliberate delay before the reply).
    TYPING_DELAY_SECONDS: float = 1.0

    # --- Owner weekly digest ---
    # Cron-style day/hour (server local time) the weekly summary is sent to
    # OWNER_WHATSAPP_NUMBER. day is one of APScheduler's day_of_week values
    # (mon/tue/.../sun). If your deployment platform doesn't run in the business's
    # local timezone, adjust the hour accordingly (e.g. Lagos is UTC+1).
    WEEKLY_DIGEST_DAY: str = "mon"
    WEEKLY_DIGEST_HOUR: int = 8

    # --- Admin dashboard ---
    # Protects /admin/orders (see app/admin.py). Blank means the dashboard is
    # fully disabled (503), never silently open — this page shows customer
    # names, phone numbers, and delivery addresses.
    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.WHATSAPP_ACCESS_TOKEN and self.WHATSAPP_PHONE_NUMBER_ID)

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY)

    @property
    def deepgram_configured(self) -> bool:
        return bool(self.DEEPGRAM_API_KEY)


@lru_cache
def get_settings() -> Settings:
    return Settings()
