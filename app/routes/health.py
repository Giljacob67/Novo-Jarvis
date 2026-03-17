from fastapi import APIRouter

from app.schemas import HealthResponse
from app.config import settings
from app.services import telegram_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/debug-telegram")
async def debug_telegram():
    db_status = "ok"
    try:
        from app.db import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {str(e)}"

    try:
        webhook_info = await telegram_service.get_webhook_info()
    except Exception as e:
        webhook_info = {"error": str(e)}

    return {
        "status": "online",
        "database": db_status,
        "config": {
            "base_url": settings.effective_base_url,
            "bot_token_configured": bool(settings.telegram_bot_token),
            "webhook_secret_configured": bool(settings.telegram_webhook_secret),
            "allowed_user_id": settings.telegram_allowed_user_id,
            "openai_api_key_configured": bool(settings.openai_api_key),
            "audio_api_key_configured": bool(settings.audio_api_key),
            "audio_base_url": settings.audio_base_url,
            "openai_transcribe_model": settings.openai_transcribe_model,
            "openai_tts_model": settings.openai_tts_model,
            "voice_responses_enabled": settings.voice_responses_enabled,
            "openai_model": settings.openai_model,
            "llm_provider": settings.llm_provider,
            "browser_automation_enabled": settings.browser_automation_enabled,
            "browser_allowed_domains_configured": bool(settings.browser_allowed_domains.strip()),
            "browser_allowed_domains": settings.browser_allowed_domains,
        },
        "telegram_webhook_info": webhook_info
    }
