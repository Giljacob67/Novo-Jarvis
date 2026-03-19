import json
import logging
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Header, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import get_db
from app.schemas.telegram import TelegramUpdate, TelegramWebhookResponse
from app.models.processed_update import ProcessedTelegramUpdate
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.action_log import ActionLog
from app.models.voice_message_log import VoiceMessageLog
from app.services import telegram_service
from app.services.assistant_service import (
    handle_free_text,
    _get_or_create_conversation,
)
from app.services.memory_service import save_memory, list_memories
from app.services import google_oauth_service
from app.services import google_calendar as google_calendar_service
from app.services import google_tasks as google_tasks_service
from app.services import google_gmail_service
from app.services import google_drive_service
from app.services import image_service
from app.services import audio_service
from app.services import autonomy_service
from app.services import executive_service
from app.services import approval_service
from app.services import proactive_service
from app.services import workflow_service
from app.services import browser_service
from app.services import news_service
from app.utils.date_utils import parse_datetime_local
from app.utils.gmail_utils import format_messages_list_telegram

logger = logging.getLogger(__name__)

router = APIRouter()

HELP_TEXT = (
    "Comandos disponíveis:\n"
    "/myday — resumo do dia\n"
    "/briefing — briefing matinal\n"
    "/briefingnow — briefing executivo agora\n"
    "/checkin — checkpoint executivo de meio-dia\n"
    "/focus — top 3 prioridades agora\n"
    "/headlines [tema] — manchetes automáticas (tecnologia, IA, Brasil, Mundo)\n"
    "/review — fechamento do dia\n"
    "/remember <texto> — salvar uma anotação\n"
    "/memories — listar anotações recentes\n"
    "/connectgoogle — conectar conta Google\n"
    "/google — status da conexão Google\n"
    "/tasks — listar tarefas\n"
    "/newtask <titulo> — criar tarefa\n"
    "/deltask <id> — excluir tarefa\n"
    "/newevent <titulo> | <inicio> | <fim> — criar evento\n"
    "/delevent <id> — excluir evento\n"
    "/inbox — e-mails recentes da inbox\n"
    "/emailsearch <consulta> — buscar e-mails\n"
    "/thread <thread_id> — ver thread de e-mail\n"
    "/drafts — listar rascunhos\n"
    "/draftemail <para> | <assunto> | <corpo> — criar rascunho\n"
    "/replydraft <message_id> | <corpo> — responder e-mail (rascunho)\n"
    "/senddraft <draft_id> — enviar rascunho\n"
    "/inboxsummary — resumo da inbox\n"
    "/drive — listar arquivos recentes do Drive\n"
    "/drivesearch <consulta> — buscar arquivos no Drive\n"
    "/drivefile <file_id> — detalhes de um arquivo no Drive\n"
    "/drivesummary <file_id> [| foco] — resumir arquivo do Drive\n"
    "/approvals — ver aprovações pendentes\n"
    "/approve <id> — aprovar uma ação\n"
    "/reject <id> — rejeitar uma ação\n"
    "/playbooks — ver workflows disponíveis\n"
    "/runworkflow <nome> [| params] — executar workflow\n"
    "/routineon <tipo> — ativar rotina\n"
    "/routineoff <tipo> — desativar rotina\n"
    "/routinestatus — status das rotinas\n"
    "/quieton — ativar quiet hours\n"
    "/quietoff — desativar quiet hours\n"
    "/quietstatus — status do quiet hours\n"
    "/autonomy [conservative|hybrid_safe|aggressive] — ver/ajustar autonomia\n"
    "/proactivestatus — status das rotinas e gatilhos proativos\n"
    "/voiceon — ativar respostas por áudio\n"
    "/voiceoff — desativar respostas por áudio\n"
    "/voicestatus — status das respostas por áudio\n"
    "🌐 Browser (automação supervisionada):\n"
    "/browserstart <url> — iniciar sessão de browser\n"
    "/browserstatus — status da sessão ativa\n"
    "/browsersessions — listar sessões recentes\n"
    "/browserclose <session_id> — encerrar sessão\n"
    "/browserresume <session_id> — retomar após login\n"
    "/webresearch <url> — pesquisar página web\n"
    "/portcheck <url> — verificar portal web\n"
    "/formsession <url> — iniciar sessão de formulário\n"
    "/browserartifacts <session_id> — ver artefatos da sessão\n"
    "/help — ver esta mensagem\n\n"
    "Ou envie texto livre ou nota de voz para conversar comigo!"
)

START_TEXT = (
    "Olá! 👋 Sou o Jarvis, seu assistente pessoal de produtividade.\n\n"
    f"{HELP_TEXT}"
)


def _gmail_not_ready_msg(db: Session, user_id: str) -> str | None:
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return "❌ Google não conectado. Use /connectgoogle para conectar sua conta primeiro."
    if not status.get("gmail_enabled"):
        return (
            "⚠️ Sua conta Google está conectada, mas sem permissões de Gmail. "
            "Use /connectgoogle para reconectar com os escopos de Gmail."
        )
    return None


def _tasks_not_ready_msg(db: Session, user_id: str) -> str | None:
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return "❌ Google não conectado. Use /connectgoogle para conectar sua conta primeiro."
    if not status.get("tasks_enabled"):
        return (
            "⚠️ Sua conta Google está conectada, mas sem permissão do Google Tasks. "
            "Use /connectgoogle para reconectar e autorizar Tasks."
        )
    return None


def _drive_not_ready_msg(db: Session, user_id: str) -> str | None:
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return "❌ Google não conectado. Use /connectgoogle para conectar sua conta primeiro."
    if not status.get("drive_enabled"):
        return (
            "⚠️ Sua conta Google está conectada, mas sem permissão do Google Drive. "
            "Use /connectgoogle para reconectar e autorizar Drive."
        )
    return None


def _log_action(db: Session, event_type: str, status: str, details: dict) -> None:
    entry = ActionLog(
        event_type=event_type,
        status=status,
        details_json=json.dumps(details, ensure_ascii=False),
    )
    db.add(entry)
    db.commit()


_PT_MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "março": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def _pick_largest_photo_file_id(photo_sizes: list) -> str | None:
    if not photo_sizes:
        return None
    best = max(
        photo_sizes,
        key=lambda p: (getattr(p, "file_size", 0) or 0, (getattr(p, "width", 0) or 0) * (getattr(p, "height", 0) or 0)),
    )
    return getattr(best, "file_id", None)


async def _extract_photo_text(file_id: str) -> str:
    try:
        img_bytes = await telegram_service.download_file(file_id)
        result = await image_service.extract_text_from_image_bytes(img_bytes)
        return (result.get("text") or "").strip()
    except Exception:
        logger.exception("Failed to extract text from Telegram photo file_id=%s", file_id)
        return ""


def _pt_date_text_to_iso(raw: str) -> str | None:
    text = (raw or "").strip().lower()
    m_slash = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", text)
    if m_slash:
        d, m, y = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
        try:
            return datetime(y, m, d).date().isoformat()
        except ValueError:
            return None

    m_pt = re.search(r"\b(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})\b", text)
    if not m_pt:
        return None
    day = int(m_pt.group(1))
    month_name = m_pt.group(2)
    year = int(m_pt.group(3))
    month = _PT_MONTHS.get(month_name)
    if not month:
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _extract_deadline_iso_from_ocr(ocr_text: str) -> str | None:
    text = ocr_text or ""
    patterns = [
        r"Último\s+Dia\s+Prazo[:\s-]+([^\n\r]+)",
        r"Data\s+Cumprimento[:\s-]+([^\n\r]+)",
        r"Prazo\s+Cumprimento[:\s-]+([^\n\r]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        iso = _pt_date_text_to_iso(m.group(1))
        if iso:
            return iso
    return _pt_date_text_to_iso(text)


def _extract_process_number(ocr_text: str) -> str:
    m = re.search(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", ocr_text or "")
    return m.group(0) if m else ""


def _should_schedule_deadline_request(text: str) -> bool:
    t = (text or "").lower()
    return (
        "agende esse prazo" in t
        or "agendar esse prazo" in t
        or ("agende" in t and "prazo" in t)
    )


async def _maybe_schedule_from_photo_deadline(db: Session, user_id: str, text: str, ocr_text: str) -> str | None:
    if not _should_schedule_deadline_request(text):
        return None
    if not (ocr_text or "").strip():
        return None

    due_iso = _extract_deadline_iso_from_ocr(ocr_text)
    if not due_iso:
        return "⚠️ Não consegui identificar a data do prazo na imagem. Tente enviar com mais nitidez ou informe a data."

    tasks_not_ready = _tasks_not_ready_msg(db, user_id)
    if tasks_not_ready:
        return tasks_not_ready

    proc = _extract_process_number(ocr_text)
    title = f"Cumprir prazo processual {proc}".strip() if proc else "Cumprir prazo processual"
    notes = "Gerado automaticamente a partir de imagem no Telegram.\n\nTrecho OCR:\n" + (ocr_text[:1200] or "(vazio)")

    result = await google_tasks_service.create_task(db, user_id, title=title, notes=notes, due=due_iso)
    if "error" in result:
        return f"❌ Não consegui agendar o prazo automaticamente: {result['error']}"
    return f"✅ Prazo agendado para {due_iso}: \"{result.get('title', title)}\""


def _get_recent_ocr_context(db: Session, user_id: str, limit: int = 12) -> str:
    conv = (
        db.query(Conversation)
        .filter(Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if not conv:
        return ""

    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.role == "user")
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    for m in msgs:
        txt = m.text or ""
        start_tag = "[TEXTO_EXTRAIDO_DA_IMAGEM]"
        end_tag = "[/TEXTO_EXTRAIDO_DA_IMAGEM]"
        if start_tag in txt and end_tag in txt:
            start = txt.find(start_tag) + len(start_tag)
            end = txt.find(end_tag)
            if end > start:
                extracted = txt[start:end].strip()
                if extracted:
                    return extracted
        if "Último Dia Prazo" in txt or "Data Cumprimento" in txt:
            return txt
    return ""


async def _handle_voice_message(
    db: Session,
    user_id: str,
    chat_id: int,
    update_id: int,
    file_id: str,
    file_unique_id: str,
    mime_type: str | None,
    duration: int | None,
    file_size: int | None,
    source_type: str,
) -> str:
    conv = _get_or_create_conversation(db, user_id)

    voice_log = VoiceMessageLog(
        user_id=user_id,
        conversation_id=conv.id,
        telegram_update_id=update_id,
        telegram_file_id=file_id,
        telegram_file_unique_id=file_unique_id,
        mime_type=mime_type,
        duration_seconds=duration,
        original_file_size=file_size,
        processing_status="received",
    )
    db.add(voice_log)
    db.commit()
    db.refresh(voice_log)

    _log_action(db, "telegram_voice_received", "success", {
        "user_id": user_id,
        "file_id": file_id,
        "source_type": source_type,
        "duration": duration,
        "file_size": file_size,
    })

    max_mb = settings.effective_max_audio_mb
    if file_size and file_size > max_mb * 1024 * 1024:
        voice_log.processing_status = "error"
        voice_log.error_message = f"Arquivo muito grande: {file_size / (1024*1024):.1f} MB"
        db.commit()
        return f"⚠️ O arquivo de áudio é muito grande ({file_size / (1024*1024):.1f} MB). O limite é {max_mb} MB."

    temp_path = None
    try:
        temp_dir = audio_service.ensure_temp_dir()
        ext = ".ogg" if source_type == "voice" else (".mp3" if not mime_type else _ext_from_mime(mime_type))
        temp_path = str(temp_dir / f"{uuid.uuid4().hex}{ext}")
        voice_log.local_temp_path = temp_path

        audio_bytes = await telegram_service.download_file(file_id)
        with open(temp_path, "wb") as f:
            f.write(audio_bytes)

        voice_log.processing_status = "transcribing"
        db.commit()

        result = await audio_service.transcribe_file(temp_path)

        if result.get("error"):
            voice_log.processing_status = "transcription_failed"
            voice_log.error_message = result["error"]
            db.commit()
            _log_action(db, "audio_transcription_failed", "error", {
                "user_id": user_id,
                "error": result["error"],
            })
            return f"❌ Não consegui transcrever o áudio: {result['error']}"

        transcription = result.get("text", "")
        voice_log.transcription_text = transcription
        voice_log.transcription_model = settings.openai_transcribe_model
        voice_log.transcription_raw_json = result.get("raw_json")
        voice_log.processing_status = "transcribed"
        db.commit()

        _log_action(db, "audio_transcribed", "success", {
            "user_id": user_id,
            "text_length": len(transcription),
            "model": settings.openai_transcribe_model,
        })

        if not transcription.strip():
            return "🎤 Recebi seu áudio, mas a transcrição ficou vazia. Pode tentar novamente ou enviar em texto?"

        reply_text = await handle_free_text(
            db, user_id, transcription,
            raw_update={"voice_log_id": voice_log.id, "source_type": source_type, "file_id": file_id, "duration": duration},
            channel="telegram_voice",
        )

        voice_log.processing_status = "completed"
        db.commit()

        transcription_note = f"🎤 _{transcription}_\n\n" if len(transcription) < 500 else "🎤 _[áudio transcrito]_\n\n"
        full_reply = f"{transcription_note}{reply_text}"

        await telegram_service.send_message(chat_id, full_reply)

        if audio_service.maybe_should_reply_with_voice(db, user_id):
            tts_ok = await _send_voice_reply(db, chat_id, reply_text, user_id)
            if tts_ok:
                voice_log.tts_generated = True
                db.commit()

        return ""

    except Exception as e:
        logger.exception("Voice processing failed for user=%s", user_id)
        voice_log.processing_status = "error"
        voice_log.error_message = str(e)
        db.commit()
        return f"❌ Erro ao processar áudio: {e}"
    finally:
        audio_service.cleanup_temp_file(temp_path)


async def _send_voice_reply(db: Session, chat_id: int, text: str, user_id: str) -> bool:
    tts_result = await audio_service.synthesize_speech(text)
    if tts_result.get("error") or not tts_result.get("audio_bytes"):
        logger.warning("TTS failed, skipping voice reply: %s", tts_result.get("error"))
        return False

    audio_bytes = tts_result["audio_bytes"]
    tts_format = tts_result.get("format", "opus")

    try:
        await telegram_service.send_voice(chat_id, audio_bytes)
        _log_action(db, "audio_reply_generated", "success", {
            "user_id": user_id,
            "method": "send_voice",
            "size": len(audio_bytes),
        })
        return True
    except Exception:
        logger.info("send_voice failed, falling back to send_audio")
        ext = tts_format if tts_format != "opus" else "ogg"
        fallback_filename = f"jarvis_response.{ext}"
        try:
            await telegram_service.send_audio(chat_id, audio_bytes, filename=fallback_filename)
            _log_action(db, "audio_reply_generated", "success", {
                "user_id": user_id,
                "method": "send_audio_fallback",
                "size": len(audio_bytes),
            })
            return True
        except Exception:
            logger.exception("send_audio fallback also failed")
            return False


def _ext_from_mime(mime_type: str) -> str:
    mime_map = {
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
    }
    return mime_map.get(mime_type, ".ogg")


def _get_routine_status(db: Session, user_id: str) -> str:
    from app.models.routine_config import RoutineConfig
    configs = db.query(RoutineConfig).filter(RoutineConfig.user_id == user_id).all()
    config_map = {c.routine_type: c.is_enabled for c in configs}

    morning = config_map.get("morning", settings.morning_briefing_enabled)
    midday = config_map.get("midday", settings.midday_checkin_enabled)
    evening = config_map.get("evening", settings.evening_review_enabled)
    reminders = config_map.get("reminders", True)

    lines = [
        "⚙️ *Status das rotinas:*",
        f"  ☀️ Briefing matinal: {'✅ ativo' if morning else '❌ desativado'} ({settings.morning_briefing_time})",
        f"  🕐 Checkpoint meio-dia: {'✅ ativo' if midday else '❌ desativado'} ({settings.midday_checkin_time})",
        f"  🌙 Fechamento do dia: {'✅ ativo' if evening else '❌ desativado'} ({settings.evening_review_time})",
        f"  🔔 Lembretes: {'✅ ativo' if reminders else '❌ desativado'}",
        f"  🤖 Proativo global: {'✅' if settings.proactive_features_enabled else '❌'}",
    ]
    return "\n".join(lines)


def _set_routine(db: Session, user_id: str, routine_type: str, enabled: bool) -> str:
    from app.models.routine_config import RoutineConfig
    config = db.query(RoutineConfig).filter(
        RoutineConfig.user_id == user_id,
        RoutineConfig.routine_type == routine_type,
    ).first()
    if config:
        config.is_enabled = enabled
    else:
        config = RoutineConfig(
            user_id=user_id,
            routine_type=routine_type,
            is_enabled=enabled,
        )
        db.add(config)
    db.commit()

    status = "ativada" if enabled else "desativada"
    labels = {
        "morning": "☀️ Briefing matinal",
        "midday": "🕐 Checkpoint meio-dia",
        "evening": "🌙 Fechamento do dia",
        "reminders": "🔔 Lembretes",
    }
    label = labels.get(routine_type, routine_type)
    return f"✅ Rotina {label} {status}."


def _check_admin_key(key: str) -> JSONResponse | None:
    if not settings.telegram_webhook_secret:
        return JSONResponse(status_code=503, content={"ok": False, "message": "TELEGRAM_WEBHOOK_SECRET not configured"})
    if key != settings.telegram_webhook_secret:
        return JSONResponse(status_code=403, content={"ok": False, "message": "Forbidden"})
    if not settings.telegram_bot_token:
        return JSONResponse(status_code=503, content={"ok": False, "message": "TELEGRAM_BOT_TOKEN not configured"})
    return None


def _mark_update_processed(db: Session, update_id: int, user_id: str) -> bool:
    """
    Persist processed update idempotently.
    Returns False when another worker already persisted the same update_id.
    """
    try:
        processed = ProcessedTelegramUpdate(update_id=update_id, user_id=str(user_id))
        db.add(processed)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        logger.info("Duplicate update_id=%s detected on persist, ignoring", update_id)
        return False


@router.post("/register")
async def register_webhook(
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
):
    err = _check_admin_key(x_admin_key)
    if err:
        return err
    base = settings.effective_base_url
    if not base:
        return JSONResponse(status_code=503, content={"ok": False, "message": "APP_BASE_URL not configured"})
    webhook_url = f"{base}/webhooks/telegram"
    result = await telegram_service.set_webhook(webhook_url, secret_token=settings.telegram_webhook_secret)
    return result


@router.get("/info")
async def webhook_info(
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
):
    err = _check_admin_key(x_admin_key)
    if err:
        return err
    result = await telegram_service.get_webhook_info()
    return result


@router.post("/telegram", response_model=TelegramWebhookResponse)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default="", alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        return JSONResponse(
            status_code=403,
            content=TelegramWebhookResponse(ok=False, message="Forbidden").model_dump(),
        )

    body = await request.json()
    try:
        update = TelegramUpdate(**body)
    except Exception as e:
        logger.error("Failed to parse Telegram update: %s", e)
        return TelegramWebhookResponse(ok=True, message="ignored")

    if update.message and update.message.from_user:
        sender_id = update.message.from_user.id
        if settings.telegram_allowed_user_id and str(sender_id) != settings.telegram_allowed_user_id:
            logger.info("Ignoring message from unauthorized user_id=%s (allowed=%s)", sender_id, settings.telegram_allowed_user_id)
            return TelegramWebhookResponse(ok=True, message="ignored")
    else:
        return TelegramWebhookResponse(ok=True, message="ignored")

    existing = db.query(ProcessedTelegramUpdate).filter(
        ProcessedTelegramUpdate.update_id == update.update_id
    ).first()
    if existing:
        logger.info("Duplicate update_id=%s, ignoring", update.update_id)
        return TelegramWebhookResponse(ok=True, message="duplicate")

    chat_id = update.message.chat.id
    user_id = str(sender_id)
    msg = update.message

    try:
        if msg.voice:
            reply = await _handle_voice_message(
                db=db, user_id=user_id, chat_id=chat_id, update_id=update.update_id,
                file_id=msg.voice.file_id, file_unique_id=msg.voice.file_unique_id,
                mime_type=msg.voice.mime_type, duration=msg.voice.duration,
                file_size=msg.voice.file_size, source_type="voice",
            )
            if reply:
                await telegram_service.send_message(chat_id, reply)
            persisted = _mark_update_processed(db, update.update_id, user_id)
            return TelegramWebhookResponse(ok=True, message="voice_processed" if persisted else "duplicate")

        if msg.audio:
            reply = await _handle_voice_message(
                db=db, user_id=user_id, chat_id=chat_id, update_id=update.update_id,
                file_id=msg.audio.file_id, file_unique_id=msg.audio.file_unique_id,
                mime_type=msg.audio.mime_type, duration=msg.audio.duration,
                file_size=msg.audio.file_size, source_type="audio",
            )
            if reply:
                await telegram_service.send_message(chat_id, reply)
            persisted = _mark_update_processed(db, update.update_id, user_id)
            return TelegramWebhookResponse(ok=True, message="audio_processed" if persisted else "duplicate")

        text = (msg.text or msg.caption or "").strip()
        ocr_text = ""
        if msg.photo:
            photo_file_id = _pick_largest_photo_file_id(msg.photo)
            if photo_file_id:
                ocr_text = await _extract_photo_text(photo_file_id)
                if ocr_text:
                    _log_action(db, "telegram_photo_ocr", "success", {
                        "user_id": user_id,
                        "update_id": update.update_id,
                        "ocr_chars": len(ocr_text),
                    })
                    scheduled_reply = await _maybe_schedule_from_photo_deadline(db, user_id, text, ocr_text)
                    if scheduled_reply:
                        await telegram_service.send_message(chat_id, scheduled_reply)
                        persisted = _mark_update_processed(db, update.update_id, user_id)
                        return TelegramWebhookResponse(ok=True, message="processed" if persisted else "duplicate")

                    if text:
                        text = (
                            f"{text}\n\n"
                            "[TEXTO_EXTRAIDO_DA_IMAGEM]\n"
                            f"{ocr_text[:4000]}\n"
                            "[/TEXTO_EXTRAIDO_DA_IMAGEM]"
                        )
                    else:
                        text = f"Analise a imagem com base no texto extraído abaixo:\n\n{ocr_text[:4000]}"

        if text and not ocr_text:
            recent_ocr = _get_recent_ocr_context(db, user_id)
            scheduled_reply = await _maybe_schedule_from_photo_deadline(db, user_id, text, recent_ocr)
            if scheduled_reply:
                await telegram_service.send_message(chat_id, scheduled_reply)
                persisted = _mark_update_processed(db, update.update_id, user_id)
                return TelegramWebhookResponse(ok=True, message="processed" if persisted else "duplicate")

        if not text:
            _mark_update_processed(db, update.update_id, user_id)
            return TelegramWebhookResponse(ok=True, message="ignored")

        reply_text = await _route_command(db, user_id, chat_id, text, body)

        if reply_text:
            await telegram_service.send_message(chat_id, reply_text)

        persisted = _mark_update_processed(db, update.update_id, user_id)
        return TelegramWebhookResponse(ok=True, message="processed" if persisted else "duplicate")
    except Exception as _exc:
        # Don't mark this update as processed on failure: Telegram will retry.
        db.rollback()
        logger.exception(
            "Telegram webhook processing failed for update_id=%s | %s: %s",
            update.update_id,
            type(_exc).__name__,
            _exc,
        )
        # Try to notify user in Telegram so the error is visible (best-effort)
        try:
            _error_preview = f"{type(_exc).__name__}: {str(_exc)[:200]}"
            await telegram_service.send_message(
                chat_id,
                f"⚠️ Erro interno ao processar sua mensagem. Detalhes nos logs do servidor.\n`{_error_preview}`",
            )
        except Exception:
            pass  # If sending the error message also fails, just ignore
        return JSONResponse(
            status_code=500,
            content=TelegramWebhookResponse(ok=False, message="temporary_error").model_dump(),
        )


async def _route_command(db: Session, user_id: str, chat_id: int, text: str, body: dict) -> str:
    if text.startswith("/start"):
        return START_TEXT
    if text.startswith("/help"):
        return HELP_TEXT

    if text.startswith("/myday"):
        return await _cmd_myday(db, user_id)
    if text.startswith("/briefingnow"):
        return await _cmd_briefingnow(db, user_id)
    if text.startswith("/checkin"):
        return await _cmd_checkin(db, user_id)
    if text.startswith("/focus"):
        return await _cmd_focus(db, user_id)
    if text.startswith("/headlines"):
        return await _cmd_headlines(db, user_id, text)
    if text.startswith("/briefing"):
        return await _cmd_briefing(db, user_id)
    if text.startswith("/review"):
        return await _cmd_review(db, user_id)

    if text.startswith("/remember"):
        note = text[len("/remember"):].strip()
        if not note:
            return "Use: /remember <sua anotação aqui>"
        save_memory(db, user_id, note, category="general", source="command")
        return f'✅ Anotação salva: "{note}"'
    if text.startswith("/memories"):
        items = list_memories(db, user_id, limit=10)
        if not items:
            return "Você ainda não tem anotações salvas. Use /remember para salvar uma."
        lines = ["📝 Suas anotações recentes:"]
        for i, m in enumerate(items, 1):
            lines.append(f"{i}. [{m.category}] {m.content}")
        return "\n".join(lines)

    if text.startswith("/approvals"):
        return _cmd_approvals(db, user_id)
    if text.startswith("/approve"):
        return await _cmd_approve(db, user_id, text)
    if text.startswith("/reject"):
        return _cmd_reject(db, user_id, text)

    if text.startswith("/playbooks"):
        return workflow_service.list_playbooks()
    if text.startswith("/runworkflow"):
        return await _cmd_runworkflow(db, user_id, text)

    if text.startswith("/routineon"):
        return _cmd_routine_toggle(db, user_id, text, True)
    if text.startswith("/routineoff"):
        return _cmd_routine_toggle(db, user_id, text, False)
    if text.startswith("/routinestatus"):
        return _get_routine_status(db, user_id)

    if text.startswith("/quieton"):
        proactive_service.set_quiet_hours_preference(db, user_id, True)
        return "🌙 Quiet hours ativadas. Sem mensagens proativas entre " + settings.quiet_hours_start + " e " + settings.quiet_hours_end + "."
    if text.startswith("/quietoff"):
        proactive_service.set_quiet_hours_preference(db, user_id, False)
        return "🔔 Quiet hours desativadas. Mensagens proativas podem chegar a qualquer hora."
    if text.startswith("/quietstatus"):
        enabled = proactive_service.get_quiet_hours_preference(db, user_id)
        global_enabled = settings.quiet_hours_enabled
        active = global_enabled and enabled
        lines = [
            "🌙 *Status do Quiet Hours:*",
            f"  Global: {'✅' if global_enabled else '❌'} ({settings.quiet_hours_start}–{settings.quiet_hours_end})",
            f"  Sua preferência: {'✅ ativo' if enabled else '❌ desativado'}",
            f"  Resultado: {'🌙 quiet hours ativas' if active else '🔔 mensagens a qualquer hora'}",
        ]
        return "\n".join(lines)
    if text.startswith("/autonomy"):
        return _cmd_autonomy(db, user_id, text)
    if text.startswith("/proactivestatus"):
        return _cmd_proactive_status(db, user_id)

    if text.startswith("/voiceon"):
        audio_service.set_voice_preference(db, user_id, True)
        if not audio_service.is_audio_configured():
            return (
                "🔊 Preferência de áudio ativada para sua conta.\n"
                "⚠️ Mas o provedor de áudio não está configurado (AUDIO_API_KEY/OPENAI_API_KEY). "
                "Peça ao administrador para configurar."
            )
        if settings.voice_responses_enabled:
            return "🔊 Respostas por áudio ativadas! Agora vou responder também com áudio quando você enviar mensagens."
        return (
            "🔊 Preferência de áudio ativada para sua conta.\n"
            "⚠️ Porém, as respostas por áudio estão desativadas globalmente (VOICE_RESPONSES_ENABLED=false). "
            "Peça ao administrador para ativar."
        )
    if text.startswith("/voiceoff"):
        audio_service.set_voice_preference(db, user_id, False)
        return "🔇 Respostas por áudio desativadas. Vou responder apenas em texto."
    if text.startswith("/voicestatus"):
        global_enabled = settings.voice_responses_enabled
        user_enabled = audio_service.get_voice_preference(db, user_id)
        audio_ready = audio_service.is_audio_configured()
        active = global_enabled and user_enabled and audio_ready
        lines = [
            "🎙️ Status de respostas por áudio:",
            f"  Global: {'✅ ativado' if global_enabled else '❌ desativado'}",
            f"  Sua preferência: {'✅ ativado' if user_enabled else '❌ desativado'}",
            f"  Credencial de áudio: {'✅ configurada' if audio_ready else '❌ ausente'}",
            f"  Modelo transcrição: {settings.openai_transcribe_model}",
            f"  Modelo TTS: {settings.openai_tts_model}",
            f"  Resultado: {'🔊 áudio ativo' if active else '🔇 apenas texto'}",
        ]
        return "\n".join(lines)
    if text.startswith("/transcribe"):
        return (
            "🎤 Para transcrever áudio, basta enviar uma nota de voz ou arquivo de áudio diretamente neste chat. "
            "O Jarvis transcreverá automaticamente e responderá."
        )

    if text.startswith("/connectgoogle"):
        return _cmd_connectgoogle(db, user_id)
    if text.startswith("/google"):
        return _cmd_google_status(db, user_id)

    if text.startswith("/tasks"):
        return await _cmd_tasks(db, user_id)
    if text.startswith("/newtask"):
        return await _cmd_newtask(db, user_id, text)
    if text.startswith("/deltask"):
        return await _cmd_deltask(db, user_id, text)
    if text.startswith("/newevent"):
        return await _cmd_newevent(db, user_id, text)
    if text.startswith("/delevent"):
        return await _cmd_delevent(db, user_id, text)

    if text.startswith("/inboxsummary"):
        return await _cmd_inboxsummary(db, user_id)
    if text.startswith("/drivesearch"):
        return await _cmd_drivesearch(db, user_id, text)
    if text.startswith("/drivesummary"):
        return await _cmd_drivesummary(db, user_id, text)
    if text.startswith("/drivefile"):
        return await _cmd_drivefile(db, user_id, text)
    if text.startswith("/drive"):
        return await _cmd_drive(db, user_id)
    if text.startswith("/inbox"):
        return await _cmd_inbox(db, user_id)
    if text.startswith("/emailsearch"):
        return await _cmd_emailsearch(db, user_id, text)
    if text.startswith("/thread"):
        return await _cmd_thread(db, user_id, text)
    if text.startswith("/draftemail"):
        return await _cmd_draftemail(db, user_id, text)
    if text.startswith("/replydraft"):
        return await _cmd_replydraft(db, user_id, text)
    if text.startswith("/senddraft"):
        return await _cmd_senddraft(db, user_id, text)
    if text.startswith("/drafts"):
        return await _cmd_drafts(db, user_id)

    if text.startswith("/browserstart"):
        return await _cmd_browserstart(db, user_id, text)
    if text.startswith("/browserstatus"):
        return await _cmd_browserstatus(db, user_id)
    if text.startswith("/browsersessions"):
        return await _cmd_browsersessions(db, user_id)
    if text.startswith("/browserclose"):
        return await _cmd_browserclose(db, user_id, text)
    if text.startswith("/browserresume"):
        return await _cmd_browserresume(db, user_id, text)
    if text.startswith("/webresearch"):
        return await _cmd_webresearch(db, user_id, text)
    if text.startswith("/portcheck"):
        return await _cmd_portcheck(db, user_id, text)
    if text.startswith("/formsession"):
        return await _cmd_formsession(db, user_id, text)
    if text.startswith("/browserartifacts"):
        return await _cmd_browserartifacts(db, user_id, text)

    return await handle_free_text(db, user_id, text, raw_update=body)


async def _cmd_myday(db: Session, user_id: str) -> str:
    card = await executive_service.build_context_card(db, user_id)
    return executive_service.compose_executive_message(
        "Resumo Executivo do Dia",
        card,
        shortcuts=["/focus", "/checkin", "/approvals"],
    )


async def _cmd_briefing(db: Session, user_id: str) -> str:
    return await proactive_service.generate_morning_briefing(db, user_id)


async def _cmd_briefingnow(db: Session, user_id: str) -> str:
    return await proactive_service.generate_morning_briefing(db, user_id)


async def _cmd_checkin(db: Session, user_id: str) -> str:
    return await proactive_service.generate_midday_checkin(db, user_id)


async def _cmd_focus(db: Session, user_id: str) -> str:
    card = await executive_service.build_context_card(db, user_id)
    return executive_service.compose_focus_message(card)


async def _cmd_headlines(db: Session, user_id: str, text: str) -> str:
    query = text[len("/headlines"):].strip()
    return await news_service.get_automatic_headlines_brief(user_text=query, per_topic=3)


async def _cmd_review(db: Session, user_id: str) -> str:
    return await proactive_service.generate_evening_review(db, user_id)


def _cmd_autonomy(db: Session, user_id: str, text: str) -> str:
    raw = text[len("/autonomy"):].strip().lower()
    if raw:
        mode = autonomy_service.set_user_autonomy_mode(db, user_id, raw)
    else:
        mode = autonomy_service.get_user_autonomy_mode(db, user_id)

    matrix = autonomy_service.autonomy_matrix(mode)
    lines = [
        f"🤖 *Autonomia atual:* `{mode}`",
        f"• Baixo risco: {matrix['baixo_risco']}",
        f"• Sensível: {matrix['sensivel']}",
        f"• Crítico: {matrix['critico']}",
        "",
        "Para alterar: /autonomy conservative | /autonomy hybrid_safe | /autonomy aggressive",
    ]
    return "\n".join(lines)


def _cmd_proactive_status(db: Session, user_id: str) -> str:
    st = proactive_service.get_proactive_status(db, user_id)
    lines = [
        "📡 *Status Proativo*",
        f"Agora: {st['now']}",
        f"Morning: {'✅' if st['morning_enabled'] else '❌'} ({st['morning_time']}) próximo {st['morning_next']}",
        f"Check-in: {'✅' if st['midday_enabled'] else '❌'} ({st['midday_time']}) próximo {st['midday_next']}",
        f"Evening: {'✅' if st['evening_enabled'] else '❌'} ({st['evening_time']}) próximo {st['evening_next']}",
        f"Gatilhos event-driven: {'✅' if st['event_triggers_enabled'] else '❌'}",
        f"Limite diário por categoria: {st['daily_limit_per_category']}",
        f"Quiet hours: {st['quiet_hours']}",
        "",
        "Envios hoje por categoria:",
    ]
    for cat, count in st["daily_counts"].items():
        lines.append(f"• {cat}: {count}")
    return "\n".join(lines)


def _cmd_approvals(db: Session, user_id: str) -> str:
    pending = approval_service.list_pending_approvals(db, user_id)
    if not pending:
        return "✅ Nenhuma aprovação pendente."
    lines = [f"⏳ *{len(pending)} aprovação(ões) pendente(s):*\n"]
    for a in pending:
        lines.append(f"*#{a.id}* — {a.title}")
        lines.append(f"  Tipo: {a.action_type}")
        lines.append(f"  {a.summary[:100]}")
        lines.append(f"  /approve {a.id} | /reject {a.id}\n")
    return "\n".join(lines)


async def _cmd_approve(db: Session, user_id: str, text: str) -> str:
    id_str = text[len("/approve"):].strip()
    if not id_str or not id_str.isdigit():
        return "Use: /approve <id>"
    approval_id = int(id_str)
    result = approval_service.approve_pending_approval(db, user_id, approval_id)
    if "error" in result:
        return f"❌ {result['error']}"
    if result.get("status") == "already_approved":
        return result["message"]

    exec_result = await approval_service.execute_approved_action(db, user_id, approval_id)
    if "error" in exec_result:
        return f"✅ Aprovação #{approval_id} aprovada.\n⚠️ Execução: {exec_result['error']}"
    if exec_result.get("status") == "already_executed":
        return f"✅ Aprovação #{approval_id} — já executada anteriormente."
    return f"✅ Aprovação #{approval_id} aprovada e executada com sucesso!"


def _cmd_reject(db: Session, user_id: str, text: str) -> str:
    id_str = text[len("/reject"):].strip()
    if not id_str or not id_str.isdigit():
        return "Use: /reject <id>"
    approval_id = int(id_str)
    result = approval_service.reject_pending_approval(db, user_id, approval_id)
    if "error" in result:
        return f"❌ {result['error']}"
    if result.get("status") == "already_rejected":
        return result["message"]
    return f"❌ Aprovação #{approval_id} rejeitada."


async def _cmd_runworkflow(db: Session, user_id: str, text: str) -> str:
    raw = text[len("/runworkflow"):].strip()
    if not raw:
        return workflow_service.list_playbooks()
    parts = [p.strip() for p in raw.split("|")]
    name = parts[0]
    params = parts[1:] if len(parts) > 1 else []
    return await workflow_service.run_workflow(db, user_id, name, params)


def _cmd_routine_toggle(db: Session, user_id: str, text: str, enabled: bool) -> str:
    cmd = "/routineon" if enabled else "/routineoff"
    routine_type = text[len(cmd):].strip().lower()
    valid = ["morning", "midday", "evening", "reminders"]
    if routine_type not in valid:
        return f"Use: {cmd} <{'|'.join(valid)}>"
    return _set_routine(db, user_id, routine_type, enabled)


def _cmd_connectgoogle(db: Session, user_id: str) -> str:
    base = settings.effective_base_url
    if not base:
        return "⚠️ URL base não está configurada. Peça ao administrador para definir APP_BASE_URL."
    if not settings.google_client_id:
        return "⚠️ Credenciais Google OAuth não configuradas. Peça ao administrador."
    auth_link = f"{base}/auth/google/start"
    status = google_oauth_service.get_status(db, user_id)
    if status.get("connected") and (not status.get("gmail_enabled") or not status.get("drive_enabled")):
        return (
            "⚠️ Sua conta Google está conectada, mas faltam permissões adicionais (Gmail e/ou Drive).\n"
            "Ao clicar no link abaixo, você será redirecionado para autorizar os escopos adicionais.\n"
            "Seus acessos anteriores (Calendar, Tasks) serão mantidos.\n\n"
            f"🔗 [Reconectar Google]({auth_link})"
        )
    return f"🔗 [Clique aqui para conectar sua conta Google]({auth_link})"


def _cmd_google_status(db: Session, user_id: str) -> str:
    status = google_oauth_service.get_status(db, user_id)
    if status.get("connected"):
        gmail_str = "✅" if status.get("gmail_enabled") else "❌"
        cal_str = "✅" if status.get("calendar_enabled") else "❌"
        tasks_str = "✅" if status.get("tasks_enabled") else "❌"
        drive_str = "✅" if status.get("drive_enabled") else "❌"
        reply = (
            "✅ Conta Google conectada!\n"
            f"Calendar: {cal_str} | Tasks: {tasks_str} | Gmail: {gmail_str} | Drive: {drive_str}\n"
            f"Validade do token: {status.get('token_expiry', 'N/A')}"
        )
        if not status.get("gmail_enabled"):
            reply += "\n\n⚠️ Gmail não autorizado. Use /connectgoogle para reconectar com escopos de Gmail."
        if not status.get("drive_enabled"):
            reply += "\n⚠️ Drive não autorizado. Use /connectgoogle para reconectar com escopo de Drive."
        mode = autonomy_service.get_user_autonomy_mode(db, user_id)
        reply += f"\n\nPerfil: {settings.message_profile} | Autonomia: {mode}"
        return reply
    return "❌ Conta Google não conectada. Use /connectgoogle para conectar."


async def _cmd_tasks(db: Session, user_id: str) -> str:
    tasks_not_ready = _tasks_not_ready_msg(db, user_id)
    if tasks_not_ready:
        return tasks_not_ready
    tasks = await google_tasks_service.list_tasks(db, user_id, limit=15)
    if not tasks:
        return "✅ Nenhuma tarefa pendente!"
    today = datetime.now().date().isoformat()
    overdue = []
    due_soon = []
    no_due = []
    for t in tasks:
        due = (t.get("due") or "")[:10]
        if due and due < today:
            overdue.append(t)
        elif due:
            due_soon.append(t)
        else:
            no_due.append(t)
    ordered = overdue + due_soon + no_due

    lines = ["📋 *Tarefas priorizadas:*"]
    if overdue:
        lines.append(f"⚠️ {len(overdue)} vencida(s).")
    for i, t in enumerate(ordered, 1):
        due_str = f" (vence: {t['due'][:10]})" if t.get("due") else ""
        lines.append(f"{i}. {t['title']}{due_str}")
    return "\n".join(lines)


async def _cmd_newtask(db: Session, user_id: str, text: str) -> str:
    title = text[len("/newtask"):].strip()
    if not title:
        return "Use: /newtask <título da tarefa>"
    tasks_not_ready = _tasks_not_ready_msg(db, user_id)
    if tasks_not_ready:
        return tasks_not_ready
    result = await google_tasks_service.create_task(db, user_id, title)
    if "error" in result:
        return f"❌ {result['error']}"
    return f'✅ Tarefa criada: "{result.get("title", title)}"'


async def _cmd_deltask(db: Session, user_id: str, text: str) -> str:
    task_id = text[len("/deltask"):].strip()
    if not task_id:
        return "Use: /deltask <id_da_tarefa>"
    tasks_not_ready = _tasks_not_ready_msg(db, user_id)
    if tasks_not_ready:
        return tasks_not_ready
    result = await google_tasks_service.delete_task(db, user_id, task_id)
    if "error" in result:
        return f"❌ {result['error']}"
    return f"✅ Tarefa excluída com sucesso."


async def _cmd_delevent(db: Session, user_id: str, text: str) -> str:
    event_id = text[len("/delevent"):].strip()
    if not event_id:
        return "Use: /delevent <id_do_evento>"
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return "❌ Google não conectado."
    result = await google_calendar_service.delete_event(db, user_id, event_id)
    if "error" in result:
        return f"❌ {result['error']}"
    return f"✅ Evento excluído com sucesso."


async def _cmd_newevent(db: Session, user_id: str, text: str) -> str:
    parts_raw = text[len("/newevent"):].strip()
    parts = [p.strip() for p in parts_raw.split("|")]
    if len(parts) < 3:
        return (
            "Use: /newevent título | início | fim\n"
            "Formato de data: YYYY-MM-DD HH:MM\n"
            "Exemplo: /newevent Reunião | 2026-03-16 09:00 | 2026-03-16 10:00"
        )
    ev_title = parts[0]
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return "❌ Google não conectado. Use /connectgoogle para conectar sua conta primeiro."
    try:
        start_dt = parse_datetime_local(parts[1], settings.timezone)
        end_dt = parse_datetime_local(parts[2], settings.timezone)
    except ValueError as e:
        return f"❌ {e}"
    result = await google_calendar_service.create_event(
        db, user_id, ev_title, start_dt, end_dt, tz=settings.timezone
    )
    if "error" in result:
        return f"❌ {result['error']}"
    reply = f'✅ Evento criado: "{result.get("title", ev_title)}"'
    if result.get("link"):
        reply += f"\n🔗 {result['link']}"
    return reply


async def _cmd_inboxsummary(db: Session, user_id: str) -> str:
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    card = await executive_service.build_context_card(db, user_id)
    emails = card.get("emails_scored", [])
    if not emails:
        return "📭 Sem e-mails relevantes no momento."

    lines = ["📬 *Inbox executiva*"]
    for i, e in enumerate(emails[:6], 1):
        urgency = e.get("urgency", "informativo")
        subject = e.get("subject", "(sem assunto)")
        sender = e.get("from", "desconhecido")
        reason = e.get("reason", "")
        icon = {"urgente": "🔴", "importante": "🟠", "informativo": "🟡", "ruído": "⚪"}.get(urgency, "🟡")
        lines.append(f"{i}. {icon} {subject} — {sender}")
        lines.append(f"   motivo: {reason}")
    lines.append("\nAtalhos: /emailsearch is:unread | /drafts")
    return "\n".join(lines)


async def _cmd_drive(db: Session, user_id: str) -> str:
    drive_not_ready = _drive_not_ready_msg(db, user_id)
    if drive_not_ready:
        return drive_not_ready

    result = await google_drive_service.list_files(db, user_id, limit=10)
    if "error" in result:
        return f"❌ {result['error']}"

    files = result.get("files", [])
    if not files:
        return "📁 Não encontrei arquivos recentes no seu Drive."

    lines = ["📁 Arquivos recentes no Drive:"]
    for i, item in enumerate(files[:10], 1):
        name = item.get("name", "(sem nome)")
        file_id = item.get("id", "")
        mime = item.get("mimeType", "")
        modified = (item.get("modifiedTime", "") or "")[:10]
        link = item.get("webViewLink", "")
        suffix = f" — {modified}" if modified else ""
        lines.append(f"{i}. {name}{suffix}")
        if file_id:
            lines.append(f"   🆔 {file_id}")
        if mime:
            lines.append(f"   tipo: {mime}")
        if link:
            lines.append(f"   🔗 {link}")
    return "\n".join(lines)


async def _cmd_drivesearch(db: Session, user_id: str, text: str) -> str:
    query = text[len("/drivesearch"):].strip()
    if not query:
        return "Use: /drivesearch <termo de busca>"

    drive_not_ready = _drive_not_ready_msg(db, user_id)
    if drive_not_ready:
        return drive_not_ready

    result = await google_drive_service.search_files(db, user_id, query=query, limit=10)
    if "error" in result:
        return f"❌ {result['error']}"

    files = result.get("files", [])
    if not files:
        return f"🔎 Nenhum arquivo encontrado para: \"{query}\""

    lines = [f"🔎 Resultados no Drive para \"{query}\":"]
    for i, item in enumerate(files[:10], 1):
        name = item.get("name", "(sem nome)")
        file_id = item.get("id", "")
        mime = item.get("mimeType", "")
        modified = (item.get("modifiedTime", "") or "")[:10]
        link = item.get("webViewLink", "")
        suffix = f" — {modified}" if modified else ""
        lines.append(f"{i}. {name}{suffix}")
        if file_id:
            lines.append(f"   🆔 {file_id}")
        if mime:
            lines.append(f"   tipo: {mime}")
        if link:
            lines.append(f"   🔗 {link}")
    return "\n".join(lines)


async def _cmd_drivefile(db: Session, user_id: str, text: str) -> str:
    file_id = text[len("/drivefile"):].strip()
    if not file_id:
        return "Use: /drivefile <file_id>"

    drive_not_ready = _drive_not_ready_msg(db, user_id)
    if drive_not_ready:
        return drive_not_ready

    result = await google_drive_service.get_file_details(db, user_id, file_id=file_id)
    if "error" in result:
        return f"❌ {result['error']}"

    item = result.get("file", {})
    name = item.get("name", "(sem nome)")
    mime = item.get("mimeType", "")
    modified = (item.get("modifiedTime", "") or "")[:19].replace("T", " ")
    link = item.get("webViewLink", "")
    size = item.get("size")
    owner = ""
    owners = item.get("owners") or []
    if owners:
        owner = owners[0].get("displayName") or owners[0].get("emailAddress", "")

    lines = [f"📄 *{name}*", f"🆔 `{item.get('id', file_id)}`"]
    if mime:
        lines.append(f"Tipo: {mime}")
    if modified:
        lines.append(f"Atualizado: {modified}")
    if size:
        lines.append(f"Tamanho: {size} bytes")
    if owner:
        lines.append(f"Dono: {owner}")
    if link:
        lines.append(f"🔗 {link}")
    return "\n".join(lines)


async def _cmd_drivesummary(db: Session, user_id: str, text: str) -> str:
    raw = text[len("/drivesummary"):].strip()
    if not raw:
        return "Use: /drivesummary <file_id> [| foco opcional]"

    parts = [p.strip() for p in raw.split("|", 1)]
    file_id = parts[0]
    focus = parts[1] if len(parts) > 1 and parts[1] else None
    if not file_id:
        return "Use: /drivesummary <file_id> [| foco opcional]"

    drive_not_ready = _drive_not_ready_msg(db, user_id)
    if drive_not_ready:
        return drive_not_ready

    result = await google_drive_service.summarize_file(db, user_id, file_id=file_id, focus=focus)
    if "error" in result:
        return f"❌ {result['error']}"

    item = result.get("file", {})
    name = item.get("name", file_id)
    summary = result.get("summary", "").strip()
    if not summary:
        return f"❌ Não consegui gerar o resumo de *{name}*."

    return f"🧠 *Resumo de {name}:*\n\n{summary}"


async def _cmd_inbox(db: Session, user_id: str) -> str:
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.list_messages(db, user_id)
    if "error" in result:
        return f"❌ {result['error']}"
    return format_messages_list_telegram(result.get("messages", []))


async def _cmd_emailsearch(db: Session, user_id: str, text: str) -> str:
    query = text[len("/emailsearch"):].strip()
    if not query:
        return (
            "Use: /emailsearch <consulta>\n"
            "Exemplos:\n"
            "  /emailsearch from:joao@email.com\n"
            "  /emailsearch is:unread subject:relatório\n"
            "  /emailsearch newer_than:3d"
        )
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.search_emails(db, user_id, query=query)
    if "error" in result:
        return f"❌ {result['error']}"
    return format_messages_list_telegram(result.get("messages", []))


async def _cmd_thread(db: Session, user_id: str, text: str) -> str:
    thread_id = text[len("/thread"):].strip()
    if not thread_id:
        return "Use: /thread <thread_id>"
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.get_thread(db, user_id, thread_id=thread_id)
    if "error" in result:
        return f"❌ {result['error']}"
    msgs = result.get("messages", [])
    if not msgs:
        return "Nenhuma mensagem nesta thread."
    lines = [f"📧 Thread ({len(msgs)} mensagens):"]
    for i, m in enumerate(msgs, 1):
        sender = m.get("from", "?")
        if "<" in sender:
            sender = sender.split("<")[0].strip().strip('"')
        subject = m.get("subject", "(sem assunto)")
        body_preview = m.get("body", "")[:200]
        lines.append(f"\n--- Mensagem {i} ---")
        lines.append(f"De: {sender}")
        lines.append(f"Assunto: {subject}")
        if body_preview:
            lines.append(f"{body_preview}")
    return "\n".join(lines)


async def _cmd_draftemail(db: Session, user_id: str, text: str) -> str:
    parts_raw = text[len("/draftemail"):].strip()
    parts = [p.strip() for p in parts_raw.split("|")]
    if len(parts) < 3:
        return (
            "Use: /draftemail destinatário | assunto | corpo\n"
            "Exemplo: /draftemail joao@email.com | Reunião amanhã | Olá João, podemos..."
        )
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.create_draft(db, user_id, to=parts[0], subject=parts[1], body=parts[2])
    if "error" in result:
        return f"❌ {result['error']}"
    return result.get("message", "Rascunho criado.")


async def _cmd_replydraft(db: Session, user_id: str, text: str) -> str:
    parts_raw = text[len("/replydraft"):].strip()
    parts = [p.strip() for p in parts_raw.split("|", 1)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return (
            "Use: /replydraft <message_id> | <corpo da resposta>\n"
            "Exemplo: /replydraft 18abc123def | Obrigado, confirmo presença!"
        )
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.create_reply_draft(db, user_id, message_id=parts[0], body=parts[1])
    if "error" in result:
        return f"❌ {result['error']}"
    return result.get("message", "Rascunho de resposta criado.")


async def _cmd_senddraft(db: Session, user_id: str, text: str) -> str:
    draft_id = text[len("/senddraft"):].strip()
    if not draft_id:
        return "Use: /senddraft <draft_id>"
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.send_draft(db, user_id, draft_id=draft_id)
    if "error" in result:
        return f"❌ {result['error']}"
    return result.get("message", "E-mail enviado!")


async def _cmd_drafts(db: Session, user_id: str) -> str:
    gmail_err = _gmail_not_ready_msg(db, user_id)
    if gmail_err:
        return gmail_err
    result = await google_gmail_service.list_drafts(db, user_id)
    if "error" in result:
        return f"❌ {result['error']}"
    drafts = result.get("drafts", [])
    if not drafts:
        return "📝 Nenhum rascunho encontrado."
    lines = ["📝 Seus rascunhos:"]
    for i, d in enumerate(drafts, 1):
        to_str = d.get("to", "?")
        subj = d.get("subject", "(sem assunto)")
        did = d.get("draft_id", "")
        lines.append(f"{i}. Para: {to_str} — {subj}\n   ID: {did}")
    return "\n".join(lines)


def _browser_not_ready_msg() -> str | None:
    from app.config import settings as s
    if not s.browser_automation_enabled:
        return "❌ Automação de browser está desativada (BROWSER_AUTOMATION_ENABLED=false)."
    if not s.browser_allowed_domains.strip():
        return (
            "❌ Nenhum domínio está na lista de permissões.\n"
            "Configure BROWSER_ALLOWED_DOMAINS no .env antes de usar automação de browser.\n"
            "Exemplo: BROWSER_ALLOWED_DOMAINS=example.com,docs.python.org"
        )
    return None


async def _cmd_browserstart(db: Session, user_id: str, text: str) -> str:
    not_ready = _browser_not_ready_msg()
    if not_ready:
        return not_ready
    url = text[len("/browserstart"):].strip()
    if not url:
        return (
            "Use: /browserstart <url>\n"
            "Exemplo: /browserstart https://example.com"
        )
    result = await browser_service.start_session(db, user_id, url)
    if "error" in result:
        return f"❌ {result['error']}"
    sid = result["session_id"]
    return (
        f"🌐 Sessão de browser iniciada!\n"
        f"ID: `{sid}`\n"
        f"URL: {url}\n\n"
        f"Comandos úteis:\n"
        f"  /browserstatus — ver status\n"
        f"  /browserclose {sid} — encerrar sessão\n"
        f"  /browserresume {sid} — retomar após login (se necessário)"
    )


async def _cmd_browserstatus(db: Session, user_id: str) -> str:
    sessions = browser_service.list_sessions(db, user_id)
    active = [s for s in sessions if s.status in ("active", "paused_for_login")]
    if not active:
        return "ℹ️ Nenhuma sessão de browser ativa no momento.\nUse /browserstart <url> para iniciar uma."
    s = active[0]
    status_icon = {"active": "✅", "paused_for_login": "⚠️"}.get(s.status, "❓")
    lines = [
        f"{status_icon} *Sessão ativa:*",
        f"  ID: `{s.session_id}`",
        f"  Status: {s.status}",
        f"  URL: {s.current_url or s.start_url or '?'}",
        f"  Título: {s.page_title or '?'}",
        f"  Passos: {s.steps_taken}",
    ]
    if s.status == "paused_for_login":
        lines.append(f"\n⚠️ Sessão pausada para login. Faça o login manualmente e use /browserresume {s.session_id}")
    return "\n".join(lines)


async def _cmd_browsersessions(db: Session, user_id: str) -> str:
    sessions = browser_service.list_sessions(db, user_id)
    if not sessions:
        return "ℹ️ Nenhuma sessão de browser encontrada."
    lines = ["🌐 *Sessões de browser (últimas 10):*"]
    for s in sessions:
        icon = {"active": "✅", "paused_for_login": "⚠️", "closed": "🔒", "expired": "⏰"}.get(s.status, "❓")
        lines.append(f"{icon} `{s.session_id}` — {s.status} — {s.current_url or s.start_url or '?'}")
    return "\n".join(lines)


async def _cmd_browserclose(db: Session, user_id: str, text: str) -> str:
    session_id = text[len("/browserclose"):].strip()
    if not session_id:
        active = [s for s in browser_service.list_sessions(db, user_id) if s.status in ("active", "paused_for_login")]
        if active:
            session_id = active[0].session_id
        else:
            return "ℹ️ Nenhuma sessão ativa. Use /browsersessions para ver todas."
    result = await browser_service.close_session(db, user_id, session_id)
    if "error" in result:
        return f"❌ {result['error']}"
    return f"🔒 Sessão `{session_id}` encerrada."


async def _cmd_browserresume(db: Session, user_id: str, text: str) -> str:
    session_id = text[len("/browserresume"):].strip()
    if not session_id:
        return "Use: /browserresume <session_id>"
    result = await browser_service.resume_session(db, user_id, session_id)
    if "error" in result:
        return f"❌ {result['error']}"
    if result.get("status") == "still_on_login":
        return result["message"]
    url = result.get("url", "?")
    title = result.get("title", "?")
    return (
        f"✅ Sessão retomada!\n"
        f"URL: {url}\n"
        f"Título: {title}\n"
        f"Agora você pode continuar a automação."
    )


async def _cmd_webresearch(db: Session, user_id: str, text: str) -> str:
    not_ready = _browser_not_ready_msg()
    if not_ready:
        return not_ready
    url = text[len("/webresearch"):].strip()
    if not url:
        return "Use: /webresearch <url>\nExemplo: /webresearch https://example.com"
    return await workflow_service.run_workflow(db, user_id, "website_research", [url])


async def _cmd_portcheck(db: Session, user_id: str, text: str) -> str:
    not_ready = _browser_not_ready_msg()
    if not_ready:
        return not_ready
    url = text[len("/portcheck"):].strip()
    if not url:
        return "Use: /portcheck <url>\nExemplo: /portcheck https://meuportal.com.br"
    return await workflow_service.run_workflow(db, user_id, "portal_check", [url])


async def _cmd_formsession(db: Session, user_id: str, text: str) -> str:
    not_ready = _browser_not_ready_msg()
    if not_ready:
        return not_ready
    rest = text[len("/formsession"):].strip()
    if not rest:
        return (
            "Use: /formsession <url> [| campo=valor ...]\n"
            "Exemplo: /formsession https://example.com/form | name=João | email=joao@x.com"
        )
    parts = [p.strip() for p in rest.split("|")]
    return await workflow_service.run_workflow(db, user_id, "form_prep", parts)


async def _cmd_browserartifacts(db: Session, user_id: str, text: str) -> str:
    session_id = text[len("/browserartifacts"):].strip()
    if not session_id:
        return "Use: /browserartifacts <session_id>"
    from app.models.browser_artifact import BrowserArtifact
    artifacts = (
        db.query(BrowserArtifact)
        .filter(
            BrowserArtifact.session_id == session_id,
            BrowserArtifact.user_id == user_id,
        )
        .order_by(BrowserArtifact.created_at.desc())
        .all()
    )
    if not artifacts:
        return f"ℹ️ Nenhum artefato encontrado para a sessão `{session_id}`."
    lines = [f"🗂 *Artefatos da sessão `{session_id}`:*"]
    for a in artifacts:
        icon = {"screenshot": "📸", "download": "📥"}.get(a.artifact_type, "📄")
        size_str = f" ({a.file_size_bytes} bytes)" if a.file_size_bytes else ""
        lines.append(f"{icon} [{a.artifact_type}] `{a.file_path or '?'}`{size_str}")
    return "\n".join(lines)
