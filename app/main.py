import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import init_db
from app.routes import api_router
from app.services import telegram_service
from app.services.scheduler_service import start_scheduler, stop_scheduler
from app.services import browser_service

logging.basicConfig(
    level=logging.DEBUG if settings.app_env == "development" else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting Jarvis Pessoal (env=%s, tz=%s)", settings.app_env, settings.timezone)
    
    # Diagnóstico de banco de dados
    db_type = "PostgreSQL" if settings.jarvis_database_url.startswith("postgresql") else "SQLite"
    logger.info("Database type detected: %s", db_type)
    
    # Diagnóstico de segurança
    if not settings.google_encryption_key:
        logger.error("!!! GOOGLE_ENCRYPTION_KEY is not set !!! Tokens will NOT be encrypted/decrypted correctly.")
    else:
        logger.info("Security check: Encryption key is configured.")

    if not settings.telegram_webhook_secret:
        logger.warning("TELEGRAM_WEBHOOK_SECRET is not set. Webhook has no extra validation layer.")

    init_db()
    logger.info("Database initialized")
    await telegram_service.start()
    logger.info("TelegramService httpx client started")

    # Registrar webhook automaticamente se BASE_URL estiver configurada
    base_url = settings.effective_base_url
    if base_url:
        webhook_url = f"{base_url}/webhooks/telegram"
        try:
            await telegram_service.set_webhook(
                webhook_url, 
                secret_token=settings.telegram_webhook_secret
            )
            logger.info("Telegram webhook registered automatically: %s", webhook_url)
        except Exception as e:
            logger.error("Failed to register Telegram webhook: %s", e)
    else:
        logger.warning("APP_BASE_URL not set — skipping automatic webhook registration")
    await start_scheduler()
    logger.info("Scheduler started")
    if settings.browser_automation_enabled:
        await browser_service.start_browser()
        logger.info("Browser service started (Playwright)")
    else:
        logger.info("Browser automation disabled — skipping Playwright launch")
    yield
    if settings.browser_automation_enabled:
        await browser_service.stop_browser()
        logger.info("Browser service stopped")
    await stop_scheduler()
    logger.info("Scheduler stopped")
    await telegram_service.stop()
    logger.info("TelegramService httpx client stopped")
    logger.info("Shutting down Jarvis Pessoal")


app = FastAPI(
    title="Jarvis Pessoal",
    description="Personal productivity assistant API",
    version="0.2.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = (time.time() - start_time) * 1000
    formatted_process_time = "{0:.2f}ms".format(process_time)
    logger.info(
        "[%s] %s - %d (%s)",
        request.method,
        request.url.path,
        response.status_code,
        formatted_process_time
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


app.include_router(api_router)
