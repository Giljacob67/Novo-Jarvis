import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.memory_item import MemoryItem

logger = logging.getLogger(__name__)

VOICE_PREF_KEY = "voice_reply_enabled"


def _get_openai_client():
    from openai import AsyncOpenAI
    kwargs: dict[str, Any] = {"api_key": settings.audio_api_key}
    if settings.audio_base_url.strip():
        kwargs["base_url"] = settings.audio_base_url.strip()
    return AsyncOpenAI(**kwargs)


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
            response = await client.audio.transcriptions.create(
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


async def synthesize_speech(text: str, voice: str | None = None, output_format: str = "ogg") -> dict[str, Any]:
    if not settings.audio_api_key:
        return {"error": "AUDIO_API_KEY/OPENAI_API_KEY não configurada.", "audio_bytes": None}

    voice = voice or settings.voice_response_voice

    try:
        client = _get_openai_client()
        response = await client.audio.speech.create(
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
