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
    try:
        info = await telegram_service.get_webhook_info()
        return {
            "config": {
                "base_url": settings.effective_base_url,
                "bot_token_configured": bool(settings.telegram_bot_token),
                "webhook_secret_configured": bool(settings.telegram_webhook_secret),
                "allowed_user_id": settings.telegram_allowed_user_id,
            },
            "telegram_webhook_info": info
        }
    except Exception as e:
        return {
            "error": str(e),
            "config": {
                "base_url": settings.effective_base_url,
                "bot_token_configured": bool(settings.telegram_bot_token),
            }
        }
