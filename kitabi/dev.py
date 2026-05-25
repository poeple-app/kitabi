"""
Local development entry point — polling mode, no webhook, no GCS, no FastAPI.

Use this for testing the bot against a real Telegram client without deploying.
Secrets come from environment variables only (HARD RULE: never written to disk).

Typical session:
    # Pull secrets into env from Secret Manager (single shell, never written
    # to a file). Adjust project flag if needed.
    export TELEGRAM_BOT_TOKEN=$(gcloud secrets versions access latest --secret=telegram-bot-token)
    export ALLOWED_TG_USER_IDS=$(gcloud secrets versions access latest --secret=allowed-tg-user-ids)
    export GEMINI_API_KEY=$(gcloud secrets versions access latest --secret=gemini-api-key)

    # Optional dev-only overrides
    export DB_PATH=./kitabi-dev.db
    export LOG_LEVEL=DEBUG

    python -m kitabi.dev

What this does NOT do:
    - No GCS backup. SQLite lives in `./kitabi-dev.db` on local disk.
    - No webhook. Telegram is polled directly.
    - No `WEBHOOK_SECRET`. Not needed in polling mode.
    - No `BOT_BASE_URL`. Not needed in polling mode.

If you also want to test the proactive nudge job locally, call
`bot.send_proactive_nudges` from a Python REPL after this is running.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import Update

from . import ai, bot, data


class DevSettings(BaseSettings):
    """Polling-mode settings. Env-only — `.env` file loading is disabled."""

    model_config = SettingsConfigDict(
        env_file=None,           # HARD RULE: no .env on disk for secrets
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: SecretStr
    allowed_tg_user_ids: list[int]
    gemini_api_key: SecretStr

    db_path: str = Field(default="./kitabi-dev.db")
    log_level: str = "DEBUG"

    @field_validator("allowed_tg_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, v: object) -> object:
        """Same multi-form parser as main.Settings — accepts int / str / list."""
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            return [int(x.strip()) for x in s.split(",") if x.strip()]
        return v


def _configure_dev_logging(level_name: str) -> None:
    """Human-readable console logs — easier than JSON for live development."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.processors.CallsiteParameterAdder(
                parameters=[
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                ]
            ),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    settings = DevSettings()
    _configure_dev_logging(settings.log_level)
    logger = structlog.get_logger("kitabi.dev")
    logger.info(
        "dev.start",
        db_path=settings.db_path,
        allowed_user_ids=settings.allowed_tg_user_ids,
    )

    # GCS disabled in dev (empty bucket name → init_gcs_backup is a no-op).
    data.init_gcs_backup("", settings.db_path)
    data.init_db(settings.db_path)
    ai.init_ai(settings.gemini_api_key.get_secret_value())

    application = bot.build_application(
        settings.telegram_bot_token.get_secret_value(),
        allowed_user_ids=settings.allowed_tg_user_ids,
        mode="polling",
    )

    # Make sure the /start command shows up in Telegram's slash menu.
    async def _post_init(app):
        await bot.set_bot_commands(app)

    application.post_init = _post_init

    logger.info("dev.polling_start")
    # `run_polling` is blocking — it manages its own asyncio loop and ctrl-c.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
