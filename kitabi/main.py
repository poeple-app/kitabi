"""
Kitabi entry point.

Responsibilities (lifecycle order):
1. Load environment configuration via pydantic-settings (`Settings`).
2. Configure structured logging (JSON, with caller-site info).
3. Build the FastAPI app with a lifespan that:
   - Restores the SQLite DB from a GCS snapshot (if configured).
     If GCS is configured but the download fails, lifespan raises so the bot
     does NOT start with an empty DB and overwrite the good snapshot.
   - Initializes the data layer (SQLite engine + tables + FTS5).
   - Initializes the AI client (Gemini).
   - Builds the python-telegram-bot Application and starts it.
   - Registers the webhook URL with Telegram.
4. Serve three endpoints:
   - `POST /webhook`     — Telegram updates (allowlist + secret-token check).
   - `POST /cron/nudge`  — daily proactive-nudge job (Cloud Scheduler).
   - `GET  /healthz`     — Cloud Run liveness probe.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import Update
from telegram.ext import Application

from . import ai, bot, data


# ──────────────────────────── settings ────────────────────────────


class Settings(BaseSettings):
    """All configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── required secrets ──
    telegram_bot_token: SecretStr
    webhook_secret: SecretStr
    allowed_tg_user_ids: list[int]
    gemini_api_key: SecretStr

    # ── optional infra config ──
    gcs_bucket_name: str = Field(default="", description="GCS bucket holding the SQLite DB.")
    bot_base_url: str | None = Field(
        default=None,
        description="Public HTTPS URL of this service (Cloud Run URL); used to set the Telegram webhook.",
    )
    db_path: str = Field(default="/data/kitabi.db", description="Path to the SQLite file.")

    # ── observability ──
    log_level: str = "INFO"
    environment: str = "production"

    @field_validator("allowed_tg_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, v: object) -> object:
        """Accept list, int, or comma/bracket string (env-friendly).

        Handles every common Secret Manager entry style:
          - `123456789`     → [123456789]   (single user id)
          - `123,456`       → [123, 456]
          - `[123, 456]`    → [123, 456]    (json-array string)
          - already int     → [123]         (pydantic-settings auto-parsed)
        """
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            return [int(x.strip()) for x in s.split(",") if x.strip()]
        return v


settings = Settings()  # Loaded eagerly so misconfiguration crashes on import.


# ──────────────────────────── logging ────────────────────────────


def configure_logging() -> None:
    """Configure structlog + stdlib logging to emit JSON with caller info."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.CallsiteParameterAdder(
                parameters=[
                    structlog.processors.CallsiteParameter.MODULE,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                ]
            ),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


configure_logging()
logger = structlog.get_logger(__name__)


# ──────────────────────────── application lifespan ────────────────────────────


_tg_app: Application | None = None
_gcs_sync_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks for the FastAPI app."""
    global _tg_app, _gcs_sync_task
    logger.info(
        "main.lifespan.startup_begin",
        environment=settings.environment,
        log_level=settings.log_level,
        db_path=settings.db_path,
        bot_base_url=settings.bot_base_url,
        gcs_bucket=settings.gcs_bucket_name or "(disabled)",
    )

    # 1) GCS backup — restore the latest snapshot BEFORE init_db.
    # If GCS_BUCKET_NAME is set but download fails, we let the exception
    # propagate. Cloud Run will restart the container; it must NOT start with
    # an empty DB that then overwrites the good snapshot on the next upload.
    try:
        data.init_gcs_backup(settings.gcs_bucket_name, settings.db_path)
    except Exception as e:
        logger.error(
            "main.gcs.restore_failed_hard",
            error=str(e),
            bucket=settings.gcs_bucket_name,
            exc_info=True,
        )
        raise  # Hard-fail: re-raise to stop container start.

    # 2) Data layer.
    data.init_db(settings.db_path)

    # 3) AI layer.
    ai.init_ai(settings.gemini_api_key.get_secret_value())

    # 4) Telegram application.
    _tg_app = bot.build_application(settings.telegram_bot_token.get_secret_value())
    await _tg_app.initialize()
    await _tg_app.start()
    await bot.set_bot_commands(_tg_app)

    # 5) Register webhook with Telegram.
    if settings.bot_base_url:
        webhook_url = f"{settings.bot_base_url.rstrip('/')}/webhook"
        try:
            await _tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=settings.webhook_secret.get_secret_value(),
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            logger.info("main.webhook.registered", url=webhook_url)
        except Exception as e:
            logger.error("main.webhook.set_failed", error=str(e), exc_info=True)
    else:
        logger.warning(
            "main.webhook.skipped",
            reason="BOT_BASE_URL not set — call set_webhook manually after deploy",
        )

    # 6) Periodic GCS sync (no-op if GCS is disabled).
    if settings.gcs_bucket_name:
        _gcs_sync_task = asyncio.create_task(data.periodic_sync_loop(60.0))
        logger.info("main.gcs_sync.task_started", interval_s=60)

    # 7) Sweep expired ephemeral state (best-effort).
    try:
        deleted = await data.purge_expired_ephemeral()
        if deleted:
            logger.info("main.ephemeral.purged", count=deleted)
    except Exception as e:
        logger.warning("main.ephemeral.purge_failed", error=str(e))

    logger.info("main.lifespan.startup_done")

    yield  # ← application serves requests

    logger.info("main.lifespan.shutdown_begin")

    # Cancel periodic sync and do one final upload
    if _gcs_sync_task is not None:
        _gcs_sync_task.cancel()
        try:
            await _gcs_sync_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("main.gcs_sync.cancel_error", error=str(e))
    if settings.gcs_bucket_name:
        try:
            ok = await asyncio.wait_for(data.sync_to_gcs(force=True), timeout=8.0)
            logger.info("main.gcs_sync.final_upload", success=ok)
        except Exception as e:
            logger.error("main.gcs_sync.final_failed", error=str(e), exc_info=True)

    if _tg_app is not None:
        try:
            await _tg_app.stop()
            await _tg_app.shutdown()
        except Exception as e:
            logger.error("main.lifespan.shutdown_error", error=str(e), exc_info=True)
    logger.info("main.lifespan.shutdown_done")


app = FastAPI(
    title="Kitabi",
    description="Personal reading-tracker Telegram bot",
    version="1.0.2",
    lifespan=lifespan,
)


# ──────────────────────────── endpoints ────────────────────────────


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe for Cloud Run / monitors."""
    return {"status": "ok", "version": "1.0.2"}


def _verify_webhook_secret(request: Request) -> None:
    """Constant-time comparison of the Telegram secret-token header."""
    sent_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    expected = settings.webhook_secret.get_secret_value()
    if not hmac.compare_digest(sent_token, expected):
        logger.warning(
            "main.webhook.bad_secret",
            sent_token_present=bool(sent_token),
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid secret token"
        )


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, str]:
    """Telegram webhook receiver."""
    _verify_webhook_secret(request)

    try:
        body = await request.json()
    except Exception as e:
        logger.error("main.webhook.bad_json", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid json"
        )

    if _tg_app is None:
        logger.error("main.webhook.app_not_ready")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready"
        )

    update = Update.de_json(body, _tg_app.bot)
    if update is None:
        logger.warning("main.webhook.unparseable", body_keys=list(body.keys()))
        return {"ok": "ignored_unparseable"}

    user = update.effective_user
    if user is None:
        logger.warning("main.webhook.no_user", update_keys=list(body.keys()))
        return {"ok": "ignored_no_user"}
    if user.id not in settings.allowed_tg_user_ids:
        logger.warning("main.webhook.user_not_allowed", user_id=user.id, username=user.username)
        return {"ok": "ignored_not_allowed"}

    try:
        await _tg_app.process_update(update)
    except Exception as e:
        logger.error(
            "main.webhook.dispatch_failed",
            error=str(e),
            user_id=user.id,
            exc_info=True,
        )
        # Always return 200 — Telegram retries on non-2xx, which spirals on bugs.
    return {"ok": "ok"}


@app.post("/cron/nudge")
async def cron_nudge(request: Request) -> dict[str, str]:
    """Proactive-nudge job — invoked by Cloud Scheduler.

    Reuses the webhook secret header for authentication. (Single-tenant bot;
    no need for a separate cron secret.)
    """
    _verify_webhook_secret(request)
    if _tg_app is None:
        raise HTTPException(status_code=503, detail="not ready")

    sent = await bot.send_proactive_nudges(_tg_app, settings.allowed_tg_user_ids)
    logger.info("main.cron.nudge.sent", count=sent)
    return {"ok": "ok", "sent": str(sent)}
