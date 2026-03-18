import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.memory_item import MemoryItem

logger = logging.getLogger(__name__)

VOICE_PREF_KEY = "voice_reply_enabled"
VOICE_SETTING_PREFIX = "voice_setting:"

# Valid TTS voices supported by OpenAI
VALID_TTS_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}

# Natural language → command keyword mapping for voice routing
_VOICE_COMMAND_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(meu dia|minha agenda|resumo do dia|como está meu dia)\b", re.I), "/myday"),
    (re.compile(r"\b(briefing|bom dia jarvis|me dá o briefing)\b", re.I), "/briefing"),
    (re.compile(r"\b(minhas tarefas|lista de tarefas|que tarefas tenho)\b", re.I), "/tasks"),
    (re.compile(r"\b(approva[çc][ão]|aprova[çc][ão]|pendentes de aprovação|o que precisa de aprovação)\b", re.I), "/approvals"),
    (re.compile(r"\b(e[- ]?mails?|inbox|caixa de entrada|mensagens novas)\b", re.I), "/inbox"),
    (re.compile(r"\b(foco|prioridades|o que faço agora|top 3)\b", re.I), "/focus"),
    (re.compile(r"\b(checkin|check[- ]?in|meio dia|como estou)\b", re.I), "/checkin"),
    (re.compile(r"\b(drive|meus arquivos|documentos)\b", re.I), "/drive"),
    (re.compile(r"\b(mem[oó]rias|anota[çc][ão]|o que eu te disse|o que você sabe)\b", re.I), "/memories"),
]


def _get_openai_client():
    from openai import OpenAI
    kwargs: dict[str, Any] = {"api_key": settings.audio_api_key}
    if settings.audio_base_url.strip():
        kwargs["base_url"] = settings.audio_base_url.strip()
    return OpenAI(**kwargs)


def is_audio_configured() -> bool:
    return bool(settings.audio_api_key)


async def transcribe_file(file_path: str, language: str = "pt") -> dict[str, Any]:
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    max_mb = settings.effective_max_audio_mb
    if file_size_mb > max_mb:
        return {
            "error": f"Arquivo de áudio muito grande ({file_size_mb:.1f} MB). Limite: {settings.max_audio_file_mb} MB.",
            "text": None,
        }

    if not settings.audio_api_key:
        return {
            "error": "AUDIO_API_KEY/OPENAI_API_KEY não configurada. Não é possível transcrever áudio.",
            "text": None,
        }

    try:
        client = _get_openai_client()
        with open(file_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=settings.openai_transcribe_model,
                file=audio_file,
                language=language,
            )

        text = response.text if hasattr(response, "text") else str(response)

        raw_json = None
        raw_data: dict[str, Any] = {}
        if hasattr(response, "model_dump"):
            raw_data = response.model_dump()
        elif hasattr(response, "__dict__"):
            raw_data = {k: v for k, v in response.__dict__.items() if not k.startswith("_")}

        if raw_data:
            extra_keys = {k: v for k, v in raw_data.items() if k not in ("text",)}
            if extra_keys:
                raw_json = json.dumps(extra_keys, ensure_ascii=False, default=str)

        return {
            "text": text.strip() if text else "",
            "raw_json": raw_json,
            "error": None,
        }
    except Exception as e:
        logger.exception("Transcription failed for %s", file_path)
        return {
            "error": f"Erro na transcrição: {e}",
            "text": None,
        }


async def synthesize_speech_for_voice(db: Session, user_id: str, text: str) -> dict[str, Any]:
    """Convenience wrapper: strips markdown, shortens, uses user's preferred voice."""
    clean = strip_markdown_for_tts(text)
    short = shorten_for_voice(clean, max_words=120)
    voice = get_tts_voice(db, user_id)
    return await synthesize_speech(short, voice=voice)


async def synthesize_speech(text: str, voice: str | None = None, output_format: str = "ogg") -> dict[str, Any]:
    if not settings.audio_api_key:
        return {"error": "AUDIO_API_KEY/OPENAI_API_KEY não configurada.", "audio_bytes": None}

    voice = voice or settings.voice_response_voice

    try:
        client = _get_openai_client()
        response = client.audio.speech.create(
            model=settings.openai_tts_model,
            voice=voice,
            input=text,
            response_format=output_format,
        )

        audio_bytes = response.content if hasattr(response, "content") else response.read()

        return {
            "audio_bytes": audio_bytes,
            "format": output_format,
            "error": None,
        }
    except Exception as e:
        logger.exception("TTS synthesis failed")
        return {
            "error": f"Erro na síntese de voz: {e}",
            "audio_bytes": None,
        }


def get_voice_preference(db: Session, user_id: str) -> bool:
    item = (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.category == "voice_preference",
            MemoryItem.content == VOICE_PREF_KEY,
            MemoryItem.is_active == True,
        )
        .first()
    )
    return item is not None


def set_voice_preference(db: Session, user_id: str, enabled: bool) -> None:
    existing = (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.category == "voice_preference",
            MemoryItem.content == VOICE_PREF_KEY,
        )
        .all()
    )
    for item in existing:
        db.delete(item)
    db.flush()

    if enabled:
        new_item = MemoryItem(
            user_id=user_id,
            category="voice_preference",
            content=VOICE_PREF_KEY,
            source="command",
            is_active=True,
        )
        db.add(new_item)
    db.commit()


def maybe_should_reply_with_voice(db: Session, user_id: str) -> bool:
    if not settings.voice_responses_enabled:
        return False
    return get_voice_preference(db, user_id)


def ensure_temp_dir() -> Path:
    path = Path(settings.temp_audio_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_temp_file(file_path: str | None) -> None:
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info("Cleaned up temp file: %s", file_path)
        except OSError:
            logger.warning("Failed to clean up temp file: %s", file_path)


# ── Voice-first utilities ─────────────────────────────────────────────────────


def strip_markdown_for_tts(text: str) -> str:
    """Remove Telegram/Markdown formatting so TTS audio sounds natural.

    Strips: *bold*, _italic_, `code`, ```blocks```, [links](url), headers, bullets.
    """
    # Remove code blocks first (multiline)
    text = re.sub(r"```[\s\S]*?```", "", text)
    # Inline code
    text = re.sub(r"`[^`]+`", "", text)
    # Bold/italic: *text* or _text_
    text = re.sub(r"[*_]{1,3}(.+?)[*_]{1,3}", r"\1", text)
    # Markdown links [label](url) → label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Plain URLs
    text = re.sub(r"https?://\S+", "", text)
    # Emoji-heavy lines (keep emoji but remove repetition of identical emojis)
    # Bullet points: leading • or - or * on a line
    text = re.sub(r"^\s*[•\-\*]\s+", "", text, flags=re.MULTILINE)
    # Headers: leading # or ##
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Multiple spaces / newlines → single
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def shorten_for_voice(text: str, max_words: int = 120) -> str:
    """Truncate a response to max_words for voice delivery, ending on a sentence."""
    words = text.split()
    if len(words) <= max_words:
        return text

    # Try to cut at a sentence boundary within max_words
    snippet = " ".join(words[:max_words])
    last_period = max(snippet.rfind("."), snippet.rfind("!"), snippet.rfind("?"))
    if last_period > len(snippet) // 2:
        return snippet[: last_period + 1]
    return snippet + "…"


def detect_voice_command(transcription: str) -> str | None:
    """Check if a voice transcription matches a known command pattern.

    Returns a /command string (e.g. "/myday") or None if no match.
    """
    text = transcription.strip()
    if not text:
        return None
    for pattern, cmd in _VOICE_COMMAND_PATTERNS:
        if pattern.search(text):
            return cmd
    return None


def get_tts_voice(db: Session, user_id: str) -> str:
    """Return the TTS voice the user has selected, or global default."""
    item = (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.category == "voice_preference",
            MemoryItem.content.startswith(VOICE_SETTING_PREFIX),
            MemoryItem.is_active == True,
        )
        .order_by(MemoryItem.created_at.desc())
        .first()
    )
    if item:
        voice = item.content[len(VOICE_SETTING_PREFIX):]
        if voice in VALID_TTS_VOICES:
            return voice
    return settings.voice_response_voice


def set_tts_voice(db: Session, user_id: str, voice: str) -> bool:
    """Persist the user's chosen TTS voice. Returns False if voice is invalid."""
    voice = voice.lower().strip()
    if voice not in VALID_TTS_VOICES:
        return False
    # Remove old voice settings
    old = (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.category == "voice_preference",
            MemoryItem.content.startswith(VOICE_SETTING_PREFIX),
        )
        .all()
    )
    for item in old:
        db.delete(item)
    db.flush()
    db.add(
        MemoryItem(
            user_id=user_id,
            category="voice_preference",
            content=f"{VOICE_SETTING_PREFIX}{voice}",
            source="command",
            is_active=True,
        )
    )
    db.commit()
    return True
