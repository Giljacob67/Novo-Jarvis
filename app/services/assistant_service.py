import hashlib
import json
import logging
import re
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.action_log import ActionLog
from app.prompts import format_history_context
from app.services.memory_service import save_memory, list_memories
from app.services.openai_service import OpenAIService
from app.services import google_oauth_service
from app.services import google_calendar as google_calendar_service
from app.services import google_tasks as google_tasks_service
from app.services import google_gmail_service
from app.services import google_drive_service
from app.services import autonomy_service
from app.services import conversation_state_service
from app.services import executive_service
from app.services import news_service
from app.services import proactive_service
from app.schemas.day import DayOverview, CalendarEvent, Task, Email
from app.utils.date_utils import parse_datetime_local

logger = logging.getLogger(__name__)

_openai_service = OpenAIService()


def get_mock_day_overview() -> DayOverview:
    today = date.today().isoformat()
    return DayOverview(
        date=today,
        calendar=[
            CalendarEvent(title="Daily standup", start="09:00", end="09:30", location="Google Meet"),
            CalendarEvent(title="Sprint review", start="14:00", end="15:00", location="Zoom"),
            CalendarEvent(title="Lunch with client", start="12:00", end="13:00", location="Restaurante Central"),
        ],
        tasks=[
            Task(title="Review PR #42", due=today, status="pending"),
            Task(title="Write API docs", due=today, status="in_progress"),
            Task(title="Deploy staging", due=today, status="done"),
        ],
        emails=[],
    )


async def get_real_or_mock_day_overview(db: Session, user_id: str) -> DayOverview:
    today = date.today().isoformat()
    tz = settings.timezone

    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return get_mock_day_overview()

    try:
        events = await google_calendar_service.list_today_events(db, user_id, tz)
        cal_items = [
            CalendarEvent(
                title=ev.get("title", ""),
                start=ev.get("start", ""),
                end=ev.get("end", ""),
                location=ev.get("location", ""),
            )
            for ev in events
        ]
    except Exception:
        logger.exception("Failed to get Google Calendar events, falling back to mock")
        cal_items = get_mock_day_overview().calendar

    try:
        tasks = await google_tasks_service.list_tasks(db, user_id, limit=20)
        task_items = [
            Task(
                title=t.get("title", ""),
                due=t.get("due", ""),
                status="done" if t.get("status") == "completed" else "pending",
            )
            for t in tasks
        ]
    except Exception:
        logger.exception("Failed to get Google Tasks, falling back to mock")
        task_items = get_mock_day_overview().tasks

    email_items: list[Email] = []
    if status.get("gmail_enabled"):
        try:
            priority_emails = await google_gmail_service.get_priority_emails(db, user_id, max_results=5)
            email_items = [
                Email(
                    subject=m.get("subject", "(sem assunto)"),
                    sender=m.get("from", "desconhecido"),
                    snippet=m.get("snippet", ""),
                    priority="high" if "IMPORTANT" in m.get("labelIds", []) else "normal",
                )
                for m in priority_emails
            ]
        except Exception:
            logger.exception("Failed to get priority emails")

    return DayOverview(
        date=today,
        calendar=cal_items,
        tasks=task_items,
        emails=email_items,
    )


def _approval_details_from_tool(tool_name: str, tool_args: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]] | None:
    if tool_name == "create_task":
        title = tool_args.get("title", "Nova tarefa")
        notes = tool_args.get("notes")
        due = tool_args.get("due")
        return (
            "create_followup_task",
            f"Criar tarefa: {title}",
            "Solicitação de criação de tarefa aguardando aprovação.",
            {"title": title, "notes": notes, "due": due},
        )

    if tool_name == "create_event":
        title = tool_args.get("title", "Novo evento")
        start_time = tool_args.get("start_time")
        end_time = tool_args.get("end_time")
        if not start_time or not end_time:
            return None
        return (
            "create_calendar_event_from_ai",
            f"Criar evento: {title}",
            f"Evento {title} de {start_time} até {end_time}.",
            {
                "title": title,
                "start_time": start_time,
                "end_time": end_time,
                "timezone": tool_args.get("timezone", settings.timezone),
                "description": tool_args.get("description"),
                "location": tool_args.get("location"),
            },
        )

    if tool_name == "update_task":
        task_id = tool_args.get("task_id", "")
        if not task_id:
            return None
        return (
            "update_task",
            f"Atualizar tarefa: {task_id}",
            "Solicitação de atualização de tarefa aguardando aprovação.",
            {
                "task_id": task_id,
                "title": tool_args.get("title"),
                "notes": tool_args.get("notes"),
                "due": tool_args.get("due"),
            },
        )

    if tool_name == "delete_task":
        task_id = tool_args.get("task_id", "")
        if not task_id:
            return None
        return (
            "delete_task",
            f"Excluir tarefa: {task_id}",
            "Solicitação de exclusão de tarefa aguardando aprovação.",
            {"task_id": task_id},
        )

    if tool_name == "update_event":
        event_id = tool_args.get("event_id", "")
        if not event_id:
            return None
        return (
            "update_calendar_event",
            f"Atualizar evento: {event_id}",
            "Solicitação de atualização de evento aguardando aprovação.",
            {
                "event_id": event_id,
                "title": tool_args.get("title"),
                "start_time": tool_args.get("start_time"),
                "end_time": tool_args.get("end_time"),
                "timezone": tool_args.get("timezone", settings.timezone),
                "description": tool_args.get("description"),
                "location": tool_args.get("location"),
            },
        )

    if tool_name == "delete_event":
        event_id = tool_args.get("event_id", "")
        if not event_id:
            return None
        return (
            "delete_calendar_event",
            f"Excluir evento: {event_id}",
            "Solicitação de exclusão de evento aguardando aprovação.",
            {"event_id": event_id},
        )

    if tool_name == "send_email_draft":
        draft_id = tool_args.get("draft_id", "")
        to = tool_args.get("to", "")
        subject = tool_args.get("subject", "")
        body = tool_args.get("body", "")
        return (
            "send_email_draft",
            f"Enviar e-mail: {subject or '(sem assunto)'}",
            f"Envio de e-mail para {to or 'destinatário não informado'}.",
            {"draft_id": draft_id, "to": to, "subject": subject, "body": body},
        )

    return None


def _maybe_create_autonomy_approval(
    db: Session,
    user_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
) -> Any | None:
    details = _approval_details_from_tool(tool_name, tool_args)
    if not details:
        return None

    action_type, title, summary, payload = details
    from app.services import approval_service

    raw = json.dumps(
        {"user_id": user_id, "tool_name": tool_name, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    idempotency_key = f"autonomy:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:40]}"
    return approval_service.create_pending_approval(
        db,
        user_id=user_id,
        action_type=action_type,
        title=title,
        summary=summary,
        payload=payload,
        source="autonomy_guard",
        idempotency_key=idempotency_key,
    )


def _autonomy_guard(
    db: Session | None,
    user_id: str,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if db is None:
        return None
    decision = autonomy_service.evaluate_tool_execution(db, user_id, tool_name)
    if decision.get("allow"):
        return None
    approval = _maybe_create_autonomy_approval(db, user_id, tool_name, tool_args or {})
    if approval:
        return {
            "status": "approval_created",
            "approval_id": approval.id,
            "mode": decision.get("mode", settings.autonomy_default_mode),
            "requires_confirmation": True,
            "message": (
                f"Ação sensível pausada pelo modo de autonomia. "
                f"Aprovação criada (#{approval.id}). Use /approve {approval.id} para executar."
            ),
        }
    return {
        "error": decision.get("message", "Ação sensível bloqueada."),
        "mode": decision.get("mode", settings.autonomy_default_mode),
        "requires_confirmation": True,
    }


async def tool_executor(tool_name: str, tool_args: dict[str, Any], db: Session | None, user_id: str) -> Any:
    if tool_name == "get_my_day":
        if db:
            overview = await get_real_or_mock_day_overview(db, user_id)
        else:
            overview = get_mock_day_overview()
        return overview.model_dump()

    if tool_name == "save_memory":
        note = tool_args.get("note", "")
        category = tool_args.get("category", "general")
        if db and note:
            item = save_memory(db, user_id, note, category, source="assistant")
            return {"saved": True, "id": item.id, "content": note}
        return {"saved": False, "error": "Nenhuma nota fornecida"}

    if tool_name == "list_memories":
        limit = tool_args.get("limit", 5)
        if db:
            items = list_memories(db, user_id, limit=limit)
            return [{"content": m.content, "category": m.category} for m in items]
        return []

    if tool_name == "list_tasks":
        if not db:
            return []
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        if not status.get("tasks_enabled"):
            return {"error": "Google Tasks não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Tasks."}
        limit = tool_args.get("limit", 10)
        return await google_tasks_service.list_tasks(db, user_id, limit=limit)

    if tool_name == "create_task":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        if not status.get("tasks_enabled"):
            return {"error": "Google Tasks não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Tasks."}
        title = tool_args.get("title", "")
        notes = tool_args.get("notes")
        due = tool_args.get("due")
        result = await google_tasks_service.create_task(db, user_id, title, notes=notes, due=due)
        if "error" not in result:
            _log_action(db, "create_task", "success", {"title": title, "user_id": user_id})
        return result

    if tool_name == "update_task":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado."}
        if not status.get("tasks_enabled"):
            return {"error": "Google Tasks não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Tasks."}
        task_id = tool_args.get("task_id", "")
        title = tool_args.get("title")
        notes = tool_args.get("notes")
        due = tool_args.get("due")
        result = await google_tasks_service.update_task(db, user_id, task_id, title=title, notes=notes, due=due)
        if "error" not in result:
            _log_action(db, "update_task", "success", {"task_id": task_id, "user_id": user_id})
        return result

    if tool_name == "delete_task":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado."}
        if not status.get("tasks_enabled"):
            return {"error": "Google Tasks não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Tasks."}
        task_id = tool_args.get("task_id", "")
        result = await google_tasks_service.delete_task(db, user_id, task_id)
        if "error" not in result:
            _log_action(db, "delete_task", "success", {"task_id": task_id, "user_id": user_id})
        return result

    if tool_name == "list_upcoming_events":
        if not db:
            return []
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        days = tool_args.get("days", 7)
        limit = tool_args.get("limit", 10)
        return await google_calendar_service.list_upcoming_events(db, user_id, days=days, limit=limit, tz=settings.timezone)

    if tool_name == "create_event":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        title = tool_args.get("title", "")
        tz = tool_args.get("timezone", settings.timezone)
        try:
            start_dt = parse_datetime_local(tool_args.get("start_time", ""), tz)
            end_dt = parse_datetime_local(tool_args.get("end_time", ""), tz)
        except ValueError as e:
            return {"error": str(e)}
        result = await google_calendar_service.create_event(
            db, user_id, title, start_dt, end_dt, tz=tz,
            description=tool_args.get("description"),
            location=tool_args.get("location"),
        )
        if "error" not in result:
            _log_action(db, "create_event", "success", {"title": title, "user_id": user_id})
        return result

    if tool_name == "update_event":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado."}
        event_id = tool_args.get("event_id", "")
        title = tool_args.get("title")
        tz = tool_args.get("timezone", settings.timezone)
        start_dt = None
        end_dt = None
        if tool_args.get("start_time"):
            start_dt = parse_datetime_local(tool_args["start_time"], tz)
        if tool_args.get("end_time"):
            end_dt = parse_datetime_local(tool_args["end_time"], tz)
        
        result = await google_calendar_service.update_event(
            db, user_id, event_id, title=title, start_dt=start_dt, end_dt=end_dt, tz=tz,
            description=tool_args.get("description"),
            location=tool_args.get("location"),
        )
        if "error" not in result:
            _log_action(db, "update_event", "success", {"event_id": event_id, "user_id": user_id})
        return result

    if tool_name == "delete_event":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado."}
        event_id = tool_args.get("event_id", "")
        result = await google_calendar_service.delete_event(db, user_id, event_id)
        if "error" not in result:
            _log_action(db, "delete_event", "success", {"event_id": event_id, "user_id": user_id})
        return result

    if tool_name == "get_google_connection_status":
        if not db:
            return {"connected": False}
        return google_oauth_service.get_status(db, user_id)

    if tool_name == "get_gmail_connection_status":
        if not db:
            return {"connected": False, "gmail_enabled": False}
        return google_oauth_service.get_status(db, user_id)

    if tool_name == "get_drive_connection_status":
        if not db:
            return {"connected": False, "drive_enabled": False}
        return google_oauth_service.get_status(db, user_id)

    if tool_name == "list_drive_files":
        if not db:
            return {"error": "Banco não disponível"}
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        if not status.get("drive_enabled"):
            return {"error": "Google Drive não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Drive."}
        limit = tool_args.get("limit", 10)
        return await google_drive_service.list_files(db, user_id, limit=limit)

    if tool_name == "search_drive_files":
        if not db:
            return {"error": "Banco não disponível"}
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        if not status.get("drive_enabled"):
            return {"error": "Google Drive não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Drive."}
        query = str(tool_args.get("query", "")).strip()
        if not query:
            return {"error": "Informe uma consulta para buscar arquivos no Drive."}
        limit = tool_args.get("limit", 10)
        return await google_drive_service.search_files(db, user_id, query=query, limit=limit)

    if tool_name == "get_drive_file_details":
        if not db:
            return {"error": "Banco não disponível"}
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        if not status.get("drive_enabled"):
            return {"error": "Google Drive não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Drive."}
        file_id = str(tool_args.get("file_id", "")).strip()
        if not file_id:
            return {"error": "Informe o file_id do Drive."}
        return await google_drive_service.get_file_details(db, user_id, file_id=file_id)

    if tool_name == "summarize_drive_file":
        if not db:
            return {"error": "Banco não disponível"}
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar sua conta."}
        if not status.get("drive_enabled"):
            return {"error": "Google Drive não autorizado. Use /connectgoogle para reconectar e liberar o escopo de Drive."}
        file_id = str(tool_args.get("file_id", "")).strip()
        if not file_id:
            return {"error": "Informe o file_id do Drive."}
        focus = tool_args.get("focus")
        return await google_drive_service.summarize_file(db, user_id, file_id=file_id, focus=focus)

    if tool_name == "get_inbox_summary":
        if not db:
            return {"error": "Banco não disponível"}
        max_results = tool_args.get("max_results", 5)
        return await google_gmail_service.summarize_inbox(db, user_id, max_results=max_results)

    if tool_name == "search_emails":
        if not db:
            return {"error": "Banco não disponível"}
        query = tool_args.get("query", "")
        max_results = tool_args.get("max_results", 10)
        return await google_gmail_service.search_emails(db, user_id, query=query, max_results=max_results)

    if tool_name == "get_email_thread":
        if not db:
            return {"error": "Banco não disponível"}
        thread_id = tool_args.get("thread_id", "")
        return await google_gmail_service.get_thread(db, user_id, thread_id=thread_id)

    if tool_name == "create_email_draft":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        to = tool_args.get("to", "")
        subject = tool_args.get("subject", "")
        body = tool_args.get("body", "")
        return await google_gmail_service.create_draft(db, user_id, to=to, subject=subject, body=body)

    if tool_name == "create_reply_draft":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        message_id = tool_args.get("message_id", "")
        body = tool_args.get("body", "")
        return await google_gmail_service.create_reply_draft(db, user_id, message_id=message_id, body=body)

    if tool_name == "list_email_drafts":
        if not db:
            return {"error": "Banco não disponível"}
        max_results = tool_args.get("max_results", 10)
        return await google_gmail_service.list_drafts(db, user_id, max_results=max_results)

    if tool_name == "send_email_draft":
        if not db:
            return {"error": "Banco não disponível"}
        guarded = _autonomy_guard(db, user_id, tool_name, tool_args)
        if guarded:
            return guarded
        draft_id = tool_args.get("draft_id", "")
        to = tool_args.get("to", "")
        subject = tool_args.get("subject", "")
        body = tool_args.get("body", "")
        if to and subject and body:
            result = await google_gmail_service.create_draft(db, user_id, to=to, subject=subject, body=body)
            if "error" in result:
                return result
            created_id = result.get("draft_id", "")
            return {
                "status": "draft_created",
                "draft_id": created_id,
                "message": f"Rascunho criado (ID: {created_id}). Use /senddraft {created_id} para enviar. O envio direto por texto livre não é permitido por segurança.",
            }
        if draft_id:
            return {
                "status": "draft_only",
                "draft_id": draft_id,
                "message": f"Para enviar este rascunho, use o comando /senddraft {draft_id}. O envio direto por texto livre não é permitido por segurança.",
            }
        return {
            "status": "draft_only",
            "message": "Para enviar e-mails, primeiro crie um rascunho com /draftemail e depois use /senddraft <id> para enviar.",
        }

    if tool_name == "get_pending_approvals":
        if not db:
            return []
        from app.services import approval_service
        approvals = approval_service.list_pending_approvals(db, user_id)
        return [
            {
                "id": a.id,
                "action_type": a.action_type,
                "title": a.title,
                "summary": a.summary,
                "status": a.status,
            }
            for a in approvals
        ]

    if tool_name == "create_approval":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import approval_service
        action_type = tool_args.get("action_type", "")
        title = tool_args.get("title", "")
        summary = tool_args.get("summary", "")
        payload = tool_args.get("payload", {})
        result = approval_service.create_pending_approval(
            db, user_id,
            action_type=action_type,
            title=title,
            summary=summary,
            payload=payload,
            source="assistant",
        )
        if result is None:
            return {"error": "Não foi possível criar a aprovação (limite atingido ou erro)."}
        return {
            "status": "approval_created",
            "approval_id": result.id,
            "message": f"Aprovação criada (#{result.id}). Use /approve {result.id} para aprovar ou /reject {result.id} para rejeitar.",
        }

    if tool_name == "run_workflow":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import workflow_service
        name = tool_args.get("name", "")
        params = tool_args.get("params", [])
        result_text = await workflow_service.run_workflow(db, user_id, name, params)
        return {"result": result_text}

    if tool_name == "get_morning_briefing":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import proactive_service
        briefing = await proactive_service.generate_morning_briefing(db, user_id)
        return {"briefing": briefing}

    if tool_name == "get_evening_review":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import proactive_service
        review = await proactive_service.generate_evening_review(db, user_id)
        return {"review": review}

    if tool_name == "get_proactive_suggestions":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import proactive_service
        return await proactive_service.get_proactive_suggestions(db, user_id)

    if tool_name == "browser_start_session":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        url = tool_args.get("url", "")
        return await browser_service.start_session(db, user_id, url)

    if tool_name == "browser_open_url":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        url = tool_args.get("url", "")
        return await browser_service.open_url(db, user_id, session_id, url)

    if tool_name == "browser_capture_screenshot":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        return await browser_service.capture_screenshot(db, user_id, session_id)

    if tool_name == "browser_extract_text":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        return await browser_service.extract_visible_text(db, user_id, session_id)

    if tool_name == "browser_click":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        selector = tool_args.get("selector", "")
        return await browser_service.click(db, user_id, session_id, selector)

    if tool_name == "browser_fill":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        selector = tool_args.get("selector", "")
        value = tool_args.get("value", "")
        return await browser_service.fill(db, user_id, session_id, selector, value)

    if tool_name == "browser_select_option":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        selector = tool_args.get("selector", "")
        value = tool_args.get("value", "")
        return await browser_service.select_option(db, user_id, session_id, selector, value)

    if tool_name == "browser_wait_for_selector":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        selector = tool_args.get("selector", "")
        timeout_ms = tool_args.get("timeout_ms")
        return await browser_service.wait_for_selector(db, user_id, session_id, selector, timeout_ms)

    if tool_name == "browser_download_file":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        trigger_selector = tool_args.get("trigger_selector", "")
        return await browser_service.download_file(db, user_id, session_id, trigger_selector)

    if tool_name == "browser_get_page_summary":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        return await browser_service.get_page_summary(db, user_id, session_id)

    if tool_name == "browser_list_sessions":
        if not db:
            return []
        from app.services import browser_service
        sessions = browser_service.list_sessions(db, user_id)
        return [
            {
                "session_id": s.session_id,
                "status": s.status,
                "current_url": s.current_url,
                "steps_taken": s.steps_taken,
                "created_at": str(s.created_at),
            }
            for s in sessions
        ]

    if tool_name == "browser_close_session":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        return await browser_service.close_session(db, user_id, session_id)

    if tool_name == "browser_get_elements":
        if not db:
            return {"error": "Banco não disponível"}
        from app.services import browser_service
        session_id = tool_args.get("session_id", "")
        return await browser_service.get_interactive_elements(db, user_id, session_id)

    return {"error": f"Tool '{tool_name}' não reconhecida"}


def _log_action(db: Session, event_type: str, status: str, details: dict) -> None:
    enriched = {
        "intent": None,
        "confidence": None,
        "trigger_type": None,
        "proactive_category": None,
    }
    enriched.update(details or {})
    entry = ActionLog(
        event_type=event_type,
        status=status,
        details_json=json.dumps(enriched, ensure_ascii=False),
    )
    db.add(entry)
    db.commit()


def _get_or_create_conversation(db: Session, user_id: str) -> Conversation:
    conv = (
        db.query(Conversation)
        .filter(Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if conv is None:
        conv = Conversation(user_id=user_id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


def _get_recent_messages(db: Session, conversation_id: int, limit: int) -> list[Message]:
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(msgs))


def _browser_access_effective() -> bool:
    if not settings.browser_automation_enabled:
        return False
    domains = [d.strip() for d in settings.browser_allowed_domains.split(",") if d.strip()]
    return bool(domains)


def _is_news_or_web_search_intent(text: str) -> bool:
    t = (text or "").lower()
    return any(
        k in t
        for k in [
            "notícia", "noticias", "news", "busque notícias", "buscar notícias",
            "pesquise na internet", "procure na internet", "pesquise no google",
            "busque na web", "pesquisar sobre", "manchetes", "headlines",
            "principais notícias", "principais noticias", "notícias do dia", "noticias do dia",
        ]
    )


def _is_affirmative(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"sim", "s", "ok", "claro", "pode", "pode sim", "yes", "y"}


def _looks_like_web_offer(text: str) -> bool:
    t = (text or "").lower()
    return bool(
        re.search(r"(acessar a internet|usar o google|pesquisar por você|quer que eu faça)", t)
    )


def _is_alert_dismiss_intent(text: str) -> bool:
    t = (text or "").lower()
    return any(
        k in t
        for k in (
            "excluir o alerta",
            "remover alerta",
            "apagar alerta",
            "desconsiderar alerta",
            "ignorar alerta",
            "já assinado",
            "ja assinado",
            "já resolvido",
            "ja resolvido",
            "pode tirar o alerta",
        )
    )


def _extract_latest_critical_alert_subject(messages: list[Message]) -> str | None:
    for msg in reversed(messages):
        if msg.role != "assistant":
            continue
        match = re.search(r"Alerta crítico:\s*(.+)", msg.text or "", flags=re.IGNORECASE)
        if not match:
            continue
        subject = match.group(1).strip()
        if "\n" in subject:
            subject = subject.split("\n", 1)[0].strip()
        return subject
    return None


def _extract_first_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>()]+", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).rstrip(".,;:")


async def _handle_web_research_intent(db: Session, user_id: str, text: str) -> str:
    url = _extract_first_url(text)
    if not url:
        try:
            return await news_service.get_automatic_headlines_brief(user_text=text, per_topic=3)
        except Exception:
            logger.exception("Automatic headlines mode failed")
            return "Não consegui montar as manchetes automáticas agora. Tente novamente em instantes."

    if not _browser_access_effective():
        return (
            "Para pesquisar uma URL específica, a navegação web supervisionada não está ativa aqui. "
            "Posso continuar te entregando manchetes automáticas por tema (tecnologia, IA, Brasil e Mundo)."
        )

    from app.services import workflow_service

    result = await workflow_service.run_workflow(db, user_id, "website_research", [url])
    if isinstance(result, str):
        return result
    return "❌ Não consegui concluir a pesquisa nessa URL."



def _save_message(db: Session, conversation_id: int, role: str, text: str, channel: str = "telegram", raw_json: str | None = None) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        channel=channel,
        text=text,
        raw_json=raw_json,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


async def handle_free_text(db: Session, user_id: str, text: str, raw_update: dict | None = None, channel: str = "telegram") -> str:
    conv = _get_or_create_conversation(db, user_id)
    state = conversation_state_service.update_state_from_text(db, user_id, text)
    augmented_text = conversation_state_service.augment_text_with_state(db, user_id, text)

    _save_message(
        db, conv.id, role="user", text=text,
        channel=channel,
        raw_json=json.dumps(raw_update, ensure_ascii=False) if raw_update else None,
    )

    recent = _get_recent_messages(db, conv.id, settings.context_max_messages)
    history = format_history_context(recent[:-1])

    if _is_alert_dismiss_intent(augmented_text):
        subject = _extract_latest_critical_alert_subject(recent[:-1])
        if subject:
            proactive_service.suppress_critical_email_alert(db, user_id, subject=subject, source="user_ack")
            ack_reply = (
                "Perfeito, alerta marcado como resolvido.\n"
                f"Não vou te notificar de novo sobre: {subject}"
            )
        else:
            ack_reply = (
                "Perfeito. Posso silenciar esse alerta, mas não encontrei o assunto exato no histórico recente.\n"
                "Me envie o assunto do alerta para eu bloquear com precisão."
            )
        _save_message(db, conv.id, role="assistant", text=ack_reply)
        return ack_reply

    if _is_news_or_web_search_intent(augmented_text):
        web_reply = await _handle_web_research_intent(db, user_id, augmented_text)
        _save_message(db, conv.id, role="assistant", text=web_reply)
        return web_reply

    if not _browser_access_effective():
        fallback_reply = (
            "No momento, a navegação web não está ativa aqui. "
            "Se quiser, o administrador pode habilitar browser supervisionado (domínios permitidos)."
        )
        if _is_affirmative(augmented_text):
            prev_assistant = next((m for m in reversed(recent[:-1]) if m.role == "assistant"), None)
            if prev_assistant and _looks_like_web_offer(prev_assistant.text):
                _save_message(db, conv.id, role="assistant", text=fallback_reply)
                return fallback_reply

    memories = list_memories(db, user_id, limit=settings.context_max_memories)
    if state:
        memories = [
            SimpleNamespace(
                category="context_state",
                content=(
                    f"Intento atual={state.last_intent}; entidade={state.last_entity or '-'}; "
                    f"pendência={state.pending_action or '-'}; confiança={state.confidence:.2f}"
                ),
            ),
            *memories,
        ]

    if (text or "").strip().lower() in {"resolve isso", "faça isso", "faz isso"}:
        card = await executive_service.build_context_card(db, user_id)
        actions = executive_service.suggest_next_actions(card, limit=3)
        reply = (
            "🛠️ *Plano rápido para resolver agora*\n\n"
            "*Feito (automático)*\n"
            "• Reorganizei prioridades com base no seu contexto atual.\n\n"
            "*Pendente (próximos passos)*\n"
            + "\n".join(f"• {a}" for a in actions)
            + "\n\n*Preciso de você*\n"
            "• Me diga `1`, `2` ou `3` para eu executar o próximo passo."
        )
        _save_message(db, conv.id, role="assistant", text=reply)
        return reply

    reply = await _openai_service.generate_reply(
        user_id=user_id,
        user_text=augmented_text,
        recent_messages=history,
        memories=memories,
        tool_executor=tool_executor,
        db=db,
    )

    if settings.message_profile == "executivo_interativo":
        try:
            card = await executive_service.build_context_card(db, user_id)
            reply = executive_service.ensure_actionable_tail(reply, card)
        except Exception:
            logger.exception("Failed to apply executive response composer")

    _save_message(db, conv.id, role="assistant", text=reply)

    return reply


def format_day_overview_text(overview: DayOverview) -> str:
    lines = [f"📅 Resumo do dia ({overview.date}):", ""]
    if overview.calendar:
        lines.append("📆 Agenda:")
        for ev in overview.calendar:
            loc = f" — {ev.location}" if ev.location else ""
            lines.append(f"  • {ev.start}–{ev.end}: {ev.title}{loc}")
        lines.append("")
    else:
        lines.append("📆 Nenhum evento na agenda hoje.")
        lines.append("")

    if overview.tasks:
        lines.append("✅ Tarefas:")
        for t in overview.tasks:
            status_icon = {"done": "✓", "in_progress": "⏳", "pending": "○"}.get(t.status, "○")
            lines.append(f"  {status_icon} {t.title}")
        lines.append("")
    else:
        lines.append("✅ Nenhuma tarefa pendente.")
        lines.append("")

    if overview.emails:
        lines.append("📧 E-mails prioritários:")
        for e in overview.emails:
            prio = {"high": "🔴", "normal": "🟡", "low": "⚪"}.get(e.priority, "⚪")
            lines.append(f"  {prio} {e.subject} (de {e.sender})")
    return "\n".join(lines)
