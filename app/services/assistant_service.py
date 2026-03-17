import json
import logging
from datetime import date
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
        to = tool_args.get("to", "")
        subject = tool_args.get("subject", "")
        body = tool_args.get("body", "")
        return await google_gmail_service.create_draft(db, user_id, to=to, subject=subject, body=body)

    if tool_name == "create_reply_draft":
        if not db:
            return {"error": "Banco não disponível"}
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
    entry = ActionLog(
        event_type=event_type,
        status=status,
        details_json=json.dumps(details, ensure_ascii=False),
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

    _save_message(
        db, conv.id, role="user", text=text,
        channel=channel,
        raw_json=json.dumps(raw_update, ensure_ascii=False) if raw_update else None,
    )

    recent = _get_recent_messages(db, conv.id, settings.context_max_messages)
    history = format_history_context(recent[:-1])

    memories = list_memories(db, user_id, limit=settings.context_max_memories)

    reply = await _openai_service.generate_reply(
        user_id=user_id,
        user_text=text,
        recent_messages=history,
        memories=memories,
        tool_executor=tool_executor,
        db=db,
    )

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
