import os
import logging
from urllib.parse import urlparse
from pydantic import AliasChoices, Field, model_validator

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _normalize_base_url(raw: str) -> str:
    """Normalize URL/domain to scheme + host only."""
    val = (raw or "").strip()
    if not val:
        return ""

    parsed = urlparse(val)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    # Accept plain domains from env vars (e.g. RAILWAY_PUBLIC_DOMAIN).
    if "://" not in val and "/" not in val and "." in val:
        return f"https://{val}".rstrip("/")

    return val.rstrip("/")


def _url_host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower()


def _resolve_railway_base_url() -> str:
    """Best-effort Railway public URL detection."""
    static_url = _normalize_base_url(os.environ.get("RAILWAY_STATIC_URL", ""))
    if static_url:
        return static_url

    public_domain = _normalize_base_url(os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""))
    if public_domain:
        return public_domain

    return ""


def _resolve_base_url(explicit: str) -> str:
    """
    Return effective public base URL (scheme + host only).

    Priority:
    1) APP_BASE_URL
    2) Railway auto-detected domain (RAILWAY_STATIC_URL / RAILWAY_PUBLIC_DOMAIN)
    3) Replit domain (REPLIT_DOMAINS)

    Safety rule:
    - On Railway, if APP_BASE_URL points to a different *.railway.app host than the
      current service domain, prefer Railway domain to avoid broken webhook URLs.
    """
    explicit_url = _normalize_base_url(explicit)
    railway_url = _resolve_railway_base_url()

    if railway_url:
        if not explicit_url:
            return railway_url

        explicit_host = _url_host(explicit_url)
        railway_host = _url_host(railway_url)
        if explicit_host == railway_host:
            return explicit_url

        # Respect custom domains set via APP_BASE_URL.
        if explicit_host and not explicit_host.endswith(".railway.app"):
            return explicit_url

        logger.warning(
            "APP_BASE_URL (%s) differs from Railway domain (%s). "
            "Using Railway domain to prevent invalid webhook registration.",
            explicit_url,
            railway_url,
        )
        return railway_url

    if explicit_url:
        return explicit_url

    domains = os.environ.get("REPLIT_DOMAINS", "").strip()
    if domains:
        domain = domains.split(",")[0].strip()
        return _normalize_base_url(domain)

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

    @model_validator(mode="before")
    @classmethod
    def _fix_database_url_and_openai(cls, data):
        if isinstance(data, dict):
            # Railway às vezes fornece 'postgres://' mas SQLAlchemy exige 'postgresql://'
            db_url = data.get("DATABASE_URL") or data.get("jarvis_database_url")
            if db_url and db_url.startswith("postgres://"):
                fixed = db_url.replace("postgres://", "postgresql://", 1)
                data["jarvis_database_url"] = fixed

            # Garantir modelo padrão confiável
            if not data.get("openai_model"):
                data["openai_model"] = "gpt-4o-mini"
        return data

    @property
    def effective_base_url(self) -> str:
        return _resolve_base_url(self.app_base_url)

    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "OPENAI_API_KEY",
            "OPENAIAPIKEY",
            "OPENAI_APIKEY",
            "OPENAI_KEY",
        ),
    )
    llm_provider: str = "openai"
    openrouter_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "OPENROUTER_API_KEY",
            "OPEN_ROUTER_API_KEY",
        ),
    )
    openrouter_model: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GEMINI_API_KEY",
            "GOOGLE_GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        ),
    )
    gemini_model: str = "gemini-2.5-flash-lite-preview"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    openai_max_tool_rounds: int = 3
    context_max_messages: int = 20
    context_max_memories: int = 10
    message_profile: str = "executivo_interativo"
    autonomy_default_mode: str = "hybrid_safe"

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
    openai_vision_model: str = "gpt-4o-mini"
    audio_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AUDIO_API_KEY",
            "OPENAI_API_KEY",
            "OPENAIAPIKEY",
            "OPENAI_APIKEY",
            "OPENAI_KEY",
        ),
    )
    audio_base_url: str = "https://api.openai.com/v1"
    voice_responses_enabled: bool = False
    voice_response_voice: str = "alloy"
    max_audio_file_mb: int = 19
    temp_audio_dir: str = "/tmp/jarvis_audio"

    @property
    def effective_max_audio_mb(self) -> int:
        return min(self.max_audio_file_mb, 20)

    proactive_features_enabled: bool = True
    morning_briefing_enabled: bool = True
    morning_briefing_time: str = "07:00"
    midday_checkin_enabled: bool = True
    midday_checkin_time: str = "13:00"
    evening_review_enabled: bool = True
    evening_review_time: str = "21:00"
    reminder_check_interval_minutes: int = 10
    default_timezone: str = "America/Sao_Paulo"
    approvals_enabled: bool = True
    max_pending_approvals: int = 20
    followup_default_days: int = 2
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    proactive_min_interval_minutes: int = 30
    proactive_daily_limit_per_category: int = 5
    proactive_event_triggers_enabled: bool = True
    proactive_pending_approval_alert_hours: int = 6

    google_gmail_enabled: bool = True
    google_gmail_scopes: str = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
    google_drive_enabled: bool = True
    google_drive_scopes: str = "https://www.googleapis.com/auth/drive.metadata.readonly"
    gmail_inbox_query_default: str = "in:inbox newer_than:7d"
    gmail_max_list_results: int = 10

    @property
    def all_google_scopes(self) -> str:
        scopes = self.google_oauth_scopes
        if self.google_gmail_enabled:
            scopes = f"{scopes} {self.google_gmail_scopes}"
        if self.google_drive_enabled:
            scopes = f"{scopes} {self.google_drive_scopes}"
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
