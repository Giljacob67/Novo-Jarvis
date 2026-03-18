import json
import logging
import hashlib
import re
from datetime import datetime, timezone, time, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models.action_log import ActionLog
from app.models.suggestion_log import SuggestionLog
from app.models.memory_item import MemoryItem
from app.services.memory_service import get_memories_by_context
from app.services import google_oauth_service
from app.services import google_gmail_service
from app.services import google_tasks as google_tasks_service
from app.services import google_calendar as google_calendar_service

logger = logging.getLogger(__name__)
ALERT_SUPPRESSION_CATEGORY = "alert_suppression"
ALERT_SUPPRESSION_PREFIX = "critical_email:"


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


def _normalize_subject(subject: str) -> str:
    text = (subject or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _subject_key(subject: str) -> str:
    normalized = _normalize_subject(subject)
    if not normalized:
        return ""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"subj:{digest}"


def _email_alert_keys(message_id: str | None, subject: str) -> list[str]:
    keys: list[str] = []
    msg = (message_id or "").strip()
    if msg:
        keys.append(f"msg:{msg}")
    subj_key = _subject_key(subject)
    if subj_key:
        keys.append(subj_key)
    return keys


def _get_email_alert_suppressions(db: Session, user_id: str) -> set[str]:
    rows = db.query(MemoryItem).filter(
        MemoryItem.user_id == user_id,
        MemoryItem.category == ALERT_SUPPRESSION_CATEGORY,
        MemoryItem.is_active == True,
    ).all()
    keys: set[str] = set()
    for row in rows:
        content = (row.content or "").strip()
        if content.startswith(ALERT_SUPPRESSION_PREFIX):
            keys.add(content[len(ALERT_SUPPRESSION_PREFIX):])
    return keys


def suppress_critical_email_alert(
    db: Session,
    user_id: str,
    subject: str,
    message_id: str | None = None,
    source: str = "user_ack",
) -> dict:
    keys = _email_alert_keys(message_id, subject)
    if not keys:
        return {"ok": False, "reason": "missing_subject"}

    existing = _get_email_alert_suppressions(db, user_id)
    created = 0
    for key in keys:
        if key in existing:
            continue
        db.add(
            MemoryItem(
                user_id=user_id,
                category=ALERT_SUPPRESSION_CATEGORY,
                content=f"{ALERT_SUPPRESSION_PREFIX}{key}",
                source=source,
                is_active=True,
            )
        )
        created += 1
    db.commit()
    _log_action(
        db,
        "proactive_alert_suppressed",
        "success",
        {"user_id": user_id, "keys": keys, "subject": subject, "message_id": message_id},
    )
    return {"ok": True, "created": created, "keys": keys}


def is_critical_email_alert_suppressed(db: Session, user_id: str, subject: str, message_id: str | None = None) -> bool:
    suppression = _get_email_alert_suppressions(db, user_id)
    if not suppression:
        return False
    keys = _email_alert_keys(message_id, subject)
    return any(k in suppression for k in keys)


def _now_in_tz() -> datetime:
    import zoneinfo
    tz = zoneinfo.ZoneInfo(settings.default_timezone)
    return datetime.now(tz)


def is_quiet_time(db: Session, user_id: str) -> bool:
    if not settings.quiet_hours_enabled:
        return False

    user_pref = db.query(MemoryItem).filter(
        MemoryItem.user_id == user_id,
        MemoryItem.category == "preference",
        MemoryItem.content == "quiet_hours_disabled",
        MemoryItem.is_active == True,
    ).first()
    if user_pref:
        return False

    now = _now_in_tz()
    current_time = now.time()
    start = time.fromisoformat(settings.quiet_hours_start)
    end = time.fromisoformat(settings.quiet_hours_end)

    if start > end:
        return current_time >= start or current_time < end
    return start <= current_time < end


def is_on_cooldown(db: Session, user_id: str, subject: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.proactive_min_interval_minutes)
    recent = db.query(ActionLog).filter(
        ActionLog.event_type == "proactive_message_sent",
        ActionLog.created_at >= cutoff,
    ).all()
    for entry in recent:
        if entry.details_json:
            try:
                details = json.loads(entry.details_json)
                if details.get("user_id") == user_id and details.get("subject") == subject:
                    return True
            except json.JSONDecodeError:
                continue
    return False


def _daily_category_count(db: Session, user_id: str, proactive_category: str) -> int:
    now = _now_in_tz()
    start_local = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    start_utc = start_local.astimezone(timezone.utc)
    sent = db.query(ActionLog).filter(
        ActionLog.event_type == "proactive_message_sent",
        ActionLog.created_at >= start_utc,
    ).all()
    count = 0
    for entry in sent:
        if not entry.details_json:
            continue
        try:
            data = json.loads(entry.details_json)
        except json.JSONDecodeError:
            continue
        if data.get("user_id") == user_id and data.get("proactive_category") == proactive_category:
            count += 1
    return count


def is_daily_limit_reached(db: Session, user_id: str, proactive_category: str) -> bool:
    return _daily_category_count(db, user_id, proactive_category) >= settings.proactive_daily_limit_per_category


def set_quiet_hours_preference(db: Session, user_id: str, enabled: bool) -> None:
    existing = db.query(MemoryItem).filter(
        MemoryItem.user_id == user_id,
        MemoryItem.category == "preference",
        MemoryItem.content == "quiet_hours_disabled",
    ).first()
    if enabled:
        if existing:
            existing.is_active = False
            db.commit()
    else:
        if existing:
            existing.is_active = True
            db.commit()
        else:
            item = MemoryItem(
                user_id=user_id,
                category="preference",
                content="quiet_hours_disabled",
                source="command",
            )
            db.add(item)
            db.commit()


def get_quiet_hours_preference(db: Session, user_id: str) -> bool:
    disabled = db.query(MemoryItem).filter(
        MemoryItem.user_id == user_id,
        MemoryItem.category == "preference",
        MemoryItem.content == "quiet_hours_disabled",
        MemoryItem.is_active == True,
    ).first()
    return disabled is None


def create_suggestion(
    db: Session,
    user_id: str,
    suggestion_type: str,
    title: str,
    body: str,
    source: str = "system",
) -> SuggestionLog:
    suggestion = SuggestionLog(
        user_id=user_id,
        suggestion_type=suggestion_type,
        title=title,
        body=body,
        source=source,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)

    _log_action(db, "suggestion_created", "success", {
        "suggestion_id": suggestion.id,
        "user_id": user_id,
        "suggestion_type": suggestion_type,
        "title": title,
    })
    return suggestion


async def send_proactive_message(
    db: Session,
    user_id: str,
    message: str,
    subject: str = "general",
    proactive_category: str = "general",
    trigger_type: str = "routine",
) -> bool:
    if is_quiet_time(db, user_id):
        logger.info("Skipping proactive message for user=%s: quiet hours", user_id)
        return False

    if is_on_cooldown(db, user_id, subject):
        logger.info("Skipping proactive message for user=%s: cooldown for subject=%s", user_id, subject)
        return False

    if is_daily_limit_reached(db, user_id, proactive_category):
        logger.info(
            "Skipping proactive message for user=%s: daily limit reached for category=%s",
            user_id,
            proactive_category,
        )
        return False

    from app.services import telegram_service
    chat_id = int(settings.telegram_allowed_user_id) if settings.telegram_allowed_user_id else None
    if not chat_id:
        logger.warning("No telegram_allowed_user_id configured, cannot send proactive message")
        return False

    try:
        await telegram_service.send_message(chat_id, message)
        _log_action(db, "proactive_message_sent", "success", {
            "user_id": user_id,
            "subject": subject,
            "length": len(message),
            "proactive_category": proactive_category,
            "trigger_type": trigger_type,
        })
        return True
    except Exception:
        logger.exception("Failed to send proactive message to user=%s", user_id)
        return False


async def generate_morning_briefing(db: Session, user_id: str) -> str:
    from app.services import executive_service

    card = await executive_service.build_context_card(db, user_id)
    return executive_service.compose_morning_briefing(card)


async def generate_evening_review(db: Session, user_id: str) -> str:
    from app.services import executive_service

    card = await executive_service.build_context_card(db, user_id)
    return executive_service.compose_executive_message(
        "Fechamento do Dia (21:00)",
        card,
        shortcuts=["/focus", "/myday", "/approvals"],
    )


async def generate_midday_checkin(db: Session, user_id: str) -> str:
    from app.services import executive_service

    card = await executive_service.build_context_card(db, user_id)
    return executive_service.compose_executive_message(
        "Checkpoint de Meio-Dia (13:00)",
        card,
        shortcuts=["/focus", "/myday", "/checkin"],
    )


async def check_upcoming_events(db: Session, user_id: str) -> list[dict]:
    from app.services import google_oauth_service
    from app.services import google_calendar as google_calendar_service

    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return []

    try:
        events = await google_calendar_service.list_today_events(db, user_id, settings.default_timezone)
        now = _now_in_tz()
        upcoming = []
        for ev in events:
            start_str = ev.get("start", "")
            try:
                if "T" in start_str:
                    from dateutil.parser import parse as dt_parse
                    start_dt = dt_parse(start_str)
                    if start_dt.tzinfo is None:
                        import zoneinfo
                        start_dt = start_dt.replace(tzinfo=zoneinfo.ZoneInfo(settings.default_timezone))
                    diff_minutes = (start_dt - now).total_seconds() / 60
                    if 0 < diff_minutes <= 15:
                        upcoming.append(ev)
            except Exception:
                continue
        return upcoming
    except Exception:
        logger.exception("check_upcoming_events failed")
        return []


async def check_due_tasks(db: Session, user_id: str) -> list[dict]:
    from app.services import google_oauth_service
    from app.services import google_tasks as google_tasks_service
    from datetime import date

    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return []

    try:
        tasks = await google_tasks_service.list_tasks(db, user_id, limit=20)
        today = date.today().isoformat()
        due_today = []
        for t in tasks:
            due = t.get("due", "")
            if due and due[:10] <= today and t.get("status") != "completed":
                due_today.append(t)
        return due_today
    except Exception:
        logger.exception("check_due_tasks failed")
        return []


async def check_followups(db: Session, user_id: str) -> list[MemoryItem]:
    return get_memories_by_context(db, user_id, ["followup"], limit=10)


async def check_pending_drafts(db: Session, user_id: str) -> list[dict]:
    from app.services import google_oauth_service
    from app.services import google_gmail_service

    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected") or not status.get("gmail_enabled"):
        return []

    try:
        result = await google_gmail_service.list_drafts(db, user_id, max_results=5)
        return result.get("drafts", [])
    except Exception:
        logger.exception("check_pending_drafts failed")
        return []


async def get_proactive_suggestions(db: Session, user_id: str) -> dict:
    upcoming = await check_upcoming_events(db, user_id)
    due_tasks = await check_due_tasks(db, user_id)
    followups = await check_followups(db, user_id)
    drafts = await check_pending_drafts(db, user_id)

    suggestions = []
    for ev in upcoming:
        suggestions.append(f"⏰ Evento em breve: {ev.get('title', '?')}")
    for t in due_tasks:
        suggestions.append(f"📋 Tarefa vencendo: {t['title']}")
    for m in followups[:3]:
        suggestions.append(f"📌 Follow-up: {m.content[:80]}")
    for d in drafts[:2]:
        suggestions.append(f"📝 Rascunho não enviado: {d.get('subject', '?')}")

    return {
        "upcoming_events": len(upcoming),
        "due_tasks": len(due_tasks),
        "followups": len(followups),
        "pending_drafts": len(drafts),
        "suggestions": suggestions,
    }


def check_stale_followups(db: Session, user_id: str) -> list[MemoryItem]:
    """Return follow-up memories older than stale_followup_hours that haven't been resolved."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.stale_followup_hours)
    return (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.is_active == True,  # noqa: E712
            MemoryItem.category == "followup",
            MemoryItem.created_at <= cutoff,
        )
        .order_by(MemoryItem.created_at.asc())
        .limit(3)
        .all()
    )


async def generate_pre_meeting_briefing(db: Session, user_id: str, event: dict) -> str:
    """Generate a context-rich LLM-powered pre-meeting notification."""
    title = event.get("title", "Reunião")
    location = event.get("location", "")
    start_str = event.get("start", "")

    diff_minutes = 15
    try:
        from dateutil.parser import parse as dt_parse
        import zoneinfo
        start_dt = dt_parse(start_str)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=zoneinfo.ZoneInfo(settings.default_timezone))
        diff_minutes = max(1, int((start_dt - _now_in_tz()).total_seconds() / 60))
    except Exception:
        pass

    mins_text = f"{diff_minutes} min"
    base = f"🗓️ *{title}* em {mins_text}"
    if location:
        base += f"\n📍 {location}"

    if not settings.pre_meeting_briefing_enabled:
        return base

    # Gather relevant memories
    all_memories = get_memories_by_context(
        db, user_id, ["general", "task", "followup", "project", "contact"], limit=40
    )
    title_words = [w.lower() for w in title.split() if len(w) > 3]
    relevant = [
        m for m in all_memories
        if any(kw in m.content.lower() for kw in title_words)
    ][:5]

    if not relevant:
        return base

    try:
        from openai import AsyncOpenAI
        api_key = settings.openai_api_key
        if not api_key:
            return base
        client = AsyncOpenAI(api_key=api_key)
        ctx_text = "\n".join(f"- {m.content}" for m in relevant)
        prompt = (
            f'Você é Jarvis, assistente pessoal executivo.\n'
            f'O usuário tem a reunião "{title}" em {mins_text}.'
            + (f"\nLocal: {location}" if location else "")
            + f"\n\nContexto relevante registrado:\n{ctx_text}\n\n"
            "Escreva uma notificação de pré-reunião em 3-4 linhas (português, direto):\n"
            "• Quando começa\n"
            "• 1-2 pontos do contexto que são relevantes para esta reunião\n"
            "• 1 ação rápida sugerida (se houver)\n"
            "Use emojis com moderação. Não repita o título como título. Seja acionável."
        )
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.3,
        )
        result = (response.choices[0].message.content or "").strip()
        return result if result else base
    except Exception:
        logger.warning("generate_pre_meeting_briefing LLM call failed, using base message")
        return base


async def generate_smart_email_alert(email_data: dict) -> str:
    """Generate an AI-enriched email urgency alert with a concrete suggested action."""
    subject = email_data.get("subject", "(sem assunto)")
    sender = email_data.get("from", email_data.get("sender", ""))
    snippet = (email_data.get("snippet", "") or "").strip()

    base = f"📧 *E-mail urgente:* {subject}" + (f"\nDe: {sender}" if sender else "")

    if not settings.smart_email_alerts_enabled or not snippet:
        return base

    try:
        from openai import AsyncOpenAI
        api_key = settings.openai_api_key
        if not api_key:
            return base
        client = AsyncOpenAI(api_key=api_key)
        prompt = (
            "Você é Jarvis, assistente pessoal executivo.\n"
            f"Um e-mail urgente chegou:\nAssunto: {subject}\n"
            + (f"De: {sender}\n" if sender else "")
            + f"Trecho: {snippet[:400]}\n\n"
            "Escreva um alerta em 3 linhas (português):\n"
            "1. Resumo do e-mail (o que é, de quem, por que é urgente)\n"
            "2. Prazo ou impacto (se mencionado; caso contrário omita esta linha)\n"
            "3. Ação concreta sugerida (responder, encaminhar, agendar, etc.)\n"
            "Use emojis relevantes. Seja direto e acionável. Máximo 60 palavras."
        )
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.2,
        )
        result = (response.choices[0].message.content or "").strip()
        return result if result else base
    except Exception:
        logger.warning("generate_smart_email_alert LLM call failed, using base message")
        return base


async def detect_event_driven_alerts(db: Session, user_id: str) -> list[dict]:
    if not settings.proactive_event_triggers_enabled:
        return []

    alerts: list[dict] = []
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return alerts

    # 1) E-mails críticos recentes
    if status.get("gmail_enabled"):
        try:
            from app.services import executive_service

            suppression = _get_email_alert_suppressions(db, user_id)
            result = await google_gmail_service.list_messages(
                db, user_id, query="is:unread in:inbox newer_than:1d", max_results=10
            )
            for m in result.get("messages", [])[:5]:
                sig = executive_service.classify_email_priority(m)
                if sig.urgency != "urgente":
                    continue
                subject = m.get("subject", "(sem assunto)")
                msg_id = m.get("id", "")
                keys = _email_alert_keys(msg_id, subject)
                if any(k in suppression for k in keys):
                    continue
                alerts.append(
                    {
                        "subject": f"critical_email_{msg_id}",
                        "message": f"📧 Alerta crítico: {subject}\nAção sugerida: revisar e responder agora.",
                        "proactive_category": "email_critical",
                        "trigger_type": "event",
                        # raw email data passed through for AI enrichment in scheduler
                        "email_data": {
                            "subject": subject,
                            "from": m.get("from", m.get("sender", "")),
                            "snippet": m.get("snippet", ""),
                        },
                    }
                )
        except Exception:
            logger.exception("Failed to detect critical emails")

    # 2) Tarefas D-1 / D-0
    try:
        tasks = await google_tasks_service.list_tasks(db, user_id, limit=25)
        today = _now_in_tz().date()
        for t in tasks:
            if t.get("status") == "completed":
                continue
            due = (t.get("due") or "")[:10]
            if not due:
                continue
            try:
                due_dt = datetime.fromisoformat(due).date()
            except Exception:
                continue
            delta = (due_dt - today).days
            if delta in {0, 1}:
                alerts.append(
                    {
                        "subject": f"deadline_task_{t.get('id', t.get('title', '?'))}_{delta}",
                        "message": f"⏳ Prazo de tarefa em D{delta:+d}: {t.get('title', '?')}",
                        "proactive_category": "deadline",
                        "trigger_type": "event",
                    }
                )
    except Exception:
        logger.exception("Failed to detect task deadline alerts")

    # 3) Conflitos de agenda próximos
    try:
        from app.services import executive_service

        events = await google_calendar_service.list_upcoming_events(db, user_id, days=1, limit=20, tz=settings.default_timezone)
        conflicts = executive_service.detect_calendar_conflicts(events)
        for idx, c in enumerate(conflicts[:3]):
            alerts.append(
                {
                    "subject": f"calendar_conflict_{idx}_{c.get('start', '')}",
                    "message": f"⚠️ Conflito de agenda: {c.get('a', '?')} x {c.get('b', '?')}.",
                    "proactive_category": "calendar_conflict",
                    "trigger_type": "event",
                }
            )
    except Exception:
        logger.exception("Failed to detect calendar conflicts")

    # 4) Aprovações pendentes antigas
    try:
        from app.models.pending_approval import PendingApproval

        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.proactive_pending_approval_alert_hours)
        pending_old = db.query(PendingApproval).filter(
            PendingApproval.user_id == user_id,
            PendingApproval.status == "pending",
            PendingApproval.created_at <= cutoff,
        ).limit(5).all()
        for ap in pending_old:
            alerts.append(
                {
                    "subject": f"approval_pending_{ap.id}",
                    "message": f"⏳ Aprovação pendente há mais de {settings.proactive_pending_approval_alert_hours}h: #{ap.id} {ap.title}",
                    "proactive_category": "approval_pending",
                    "trigger_type": "event",
                }
            )
    except Exception:
        logger.exception("Failed to detect pending-approval alerts")

    return alerts


def get_proactive_status(db: Session, user_id: str) -> dict:
    now = _now_in_tz()

    def _next_time(target_hhmm: str) -> str:
        target = time.fromisoformat(target_hhmm)
        candidate = datetime.combine(now.date(), target, tzinfo=now.tzinfo)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.strftime("%Y-%m-%d %H:%M")

    categories = ["morning_briefing", "midday_checkin", "evening_review", "deadline", "email_critical", "calendar_conflict", "approval_pending"]
    daily_counts = {
        cat: _daily_category_count(db, user_id, cat)
        for cat in categories
    }

    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "morning_enabled": settings.morning_briefing_enabled,
        "morning_time": settings.morning_briefing_time,
        "morning_next": _next_time(settings.morning_briefing_time),
        "midday_enabled": settings.midday_checkin_enabled,
        "midday_time": settings.midday_checkin_time,
        "midday_next": _next_time(settings.midday_checkin_time),
        "evening_enabled": settings.evening_review_enabled,
        "evening_time": settings.evening_review_time,
        "evening_next": _next_time(settings.evening_review_time),
        "event_triggers_enabled": settings.proactive_event_triggers_enabled,
        "daily_limit_per_category": settings.proactive_daily_limit_per_category,
        "daily_counts": daily_counts,
        "quiet_hours": f"{settings.quiet_hours_start}-{settings.quiet_hours_end}" if settings.quiet_hours_enabled else "disabled",
    }
