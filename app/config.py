import os
import logging
from urllib.parse import urlparse
from pydantic import Field

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)




def _resolve_base_url(explicit: str) -> str:
    """Return APP_BASE_URL (domain only), falling back to REPLIT_DOMAINS if not set."""
    val = explicit.strip()
    if not val:
        domains = os.environ.get("REPLIT_DOMAINS", "").strip()
        if domains:
            domain = domains.split(",")[0].strip()
            val = f"https://{domain}"
    
    if val:
        # Se o usuário colou a URL completa de callback, pegamos só o esquema + domínio
        parsed = urlparse(val)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return val.rstrip("/")
    return ""


class Settings(BaseSettings):
    app_env: str = "development"
    # Alias para DATABASE_URL que o Railway fornece por padrão
    jarvis_database_url: str = Field(
        default="sqlite:///./jarvis.db", 
        validation_alias="DATABASE_URL"
    )
    timezone: str = "America/Sao_Paulo"
    app_base_url: str = Field(default="", validation_alias="APP_BASE_URL")

    @property
    def effective_base_url(self) -> str:
        return _resolve_base_url(self.app_base_url)

    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_max_tool_rounds: int = 3
    context_max_messages: int = 20
    context_max_memories: int = 10

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_user_id: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    google_oauth_scopes: str = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/tasks"
    google_encryption_key: str = ""

    openai_transcribe_model: str = "gpt-4o-mini-transcribe"
    openai_tts_model: str = "gpt-4o-mini-tts"
    voice_responses_enabled: bool = False
    voice_response_voice: str = "alloy"
    max_audio_file_mb: int = 19
    temp_audio_dir: str = "/tmp/jarvis_audio"

    @property
    def effective_max_audio_mb(self) -> int:
        return min(self.max_audio_file_mb, 20)

    proactive_features_enabled: bool = True
    morning_briefing_enabled: bool = True
    morning_briefing_time: str = "08:00"
    evening_review_enabled: bool = True
    evening_review_time: str = "18:30"
    reminder_check_interval_minutes: int = 10
    default_timezone: str = "America/Sao_Paulo"
    approvals_enabled: bool = True
    max_pending_approvals: int = 20
    followup_default_days: int = 2
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    proactive_min_interval_minutes: int = 30

    google_gmail_enabled: bool = True
    google_gmail_scopes: str = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
    gmail_inbox_query_default: str = "in:inbox newer_than:7d"
    gmail_max_list_results: int = 10

    @property
    def all_google_scopes(self) -> str:
        scopes = self.google_oauth_scopes
        if self.google_gmail_enabled:
            scopes = f"{scopes} {self.google_gmail_scopes}"
        return scopes

    @property
    def effective_google_redirect_uri(self) -> str:
        if self.google_redirect_uri.strip():
            return self.google_redirect_uri
        base = self.effective_base_url
        if base:
            return f"{base}/auth/google/callback"
        return ""

    browser_automation_enabled: bool = False
    browser_headless: bool = True
    browser_default_timeout_ms: int = 15000
    browser_navigation_timeout_ms: int = 30000
    browser_allowed_domains: str = ""
    browser_session_ttl_minutes: int = 20
    browser_download_dir: str = "/tmp/jarvis_downloads"
    browser_screenshot_dir: str = "/tmp/jarvis_screens"
    browser_max_steps_per_run: int = 25
    browser_require_approval_for_submit: bool = True
    browser_allow_file_downloads: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
