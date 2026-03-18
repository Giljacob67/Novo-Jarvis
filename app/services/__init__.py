from app.config import settings
from app.services.telegram import TelegramService
from app.services.openai_service import OpenAIService
from app.services import google_oauth_service
from app.services import google_calendar as google_calendar_service
from app.services import google_tasks as google_tasks_service
from app.services import google_gmail_service
from app.services import google_drive_service
from app.services import executive_service
from app.services import autonomy_service
from app.services import conversation_state_service
from app.services import news_service

telegram_service = TelegramService(bot_token=settings.telegram_bot_token)

__all__ = [
    "telegram_service",
    "TelegramService",
    "OpenAIService",
    "google_oauth_service",
    "google_calendar_service",
    "google_tasks_service",
    "google_gmail_service",
    "google_drive_service",
    "executive_service",
    "autonomy_service",
    "conversation_state_service",
    "news_service",
]
