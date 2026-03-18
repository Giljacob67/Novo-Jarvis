from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.services import google_oauth_service
from app.services import google_calendar as google_calendar_service
from app.services import google_tasks as google_tasks_service
from app.services import google_gmail_service
from app.services import approval_service


@dataclass
class EmailSignal:
    urgency: str
    score: int
    reason: str


def _parse_dt(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _now_local() -> datetime:
    import zoneinfo

    return datetime.now(zoneinfo.ZoneInfo(settings.default_timezone))


def classify_email_priority(message: dict[str, Any]) -> EmailSignal:
    subject = (message.get("subject") or "").lower()
    sender = (message.get("from") or "").lower()
    snippet = (message.get("snippet") or "").lower()
    blob = f"{subject} {sender} {snippet}"

    urgent_kw = ("urgente", "prazo", "venc", "intima", "judicial", "falha", "error", "vpn", "aws", "security")
    noise_kw = ("newsletter", "promo", "oferta", "sale", "assinatura", "boletim", "trilhas várias")
    important_kw = ("reunião", "reuniao", "cliente", "follow-up", "followup", "proposta", "contrato")

    if any(k in blob for k in urgent_kw):
        return EmailSignal("urgente", 95, "contém sinais de urgência/prazo")
    if any(k in blob for k in important_kw):
        return EmailSignal("importante", 75, "relacionado a cliente/projeto")
    if any(k in blob for k in noise_kw):
        return EmailSignal("ruído", 20, "newsletter/promocional")
    return EmailSignal("informativo", 45, "informação geral")


def detect_calendar_conflicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[tuple[datetime, datetime, str]] = []
    for ev in events:
        start = _parse_dt(ev.get("start", ""))
        end = _parse_dt(ev.get("end", ""))
        if not start or not end:
            continue
        normalized.append((start, end, ev.get("title", "?")))

    normalized.sort(key=lambda x: x[0])
    conflicts: list[dict[str, Any]] = []
    for i in range(len(normalized) - 1):
        a_start, a_end, a_title = normalized[i]
        b_start, b_end, b_title = normalized[i + 1]
        if a_end > b_start:
            conflicts.append(
                {
                    "a": a_title,
                    "b": b_title,
                    "start": b_start.isoformat(),
                }
            )
    return conflicts


async def build_context_card(db: Session, user_id: str) -> dict[str, Any]:
    now = _now_local()
    horizon = now + timedelta(hours=24)
    status = google_oauth_service.get_status(db, user_id)

    events: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    emails: list[dict[str, Any]] = []

    if status.get("connected"):
        try:
            raw_events = await google_calendar_service.list_upcoming_events(
                db, user_id, days=2, limit=25, tz=settings.default_timezone
            )
            for ev in raw_events:
                start = _parse_dt(ev.get("start", ""))
                if start and now <= start <= horizon:
                    events.append(ev)
        except Exception:
            events = []

        try:
            tasks = await google_tasks_service.list_tasks(db, user_id, limit=30)
        except Exception:
            tasks = []

        if status.get("gmail_enabled"):
            try:
                result = await google_gmail_service.list_messages(
                    db, user_id, query=settings.gmail_inbox_query_default, max_results=15
                )
                emails = result.get("messages", []) if "error" not in result else []
            except Exception:
                emails = []

    conflicts = detect_calendar_conflicts(events)

    today = now.date().isoformat()
    overdue_tasks: list[dict[str, Any]] = []
    due_soon_tasks: list[dict[str, Any]] = []
    for task in tasks:
        due = (task.get("due") or "")[:10]
        if not due or task.get("status") == "completed":
            continue
        if due < today:
            overdue_tasks.append(task)
        elif due <= (now.date() + timedelta(days=1)).isoformat():
            due_soon_tasks.append(task)

    scored_emails = []
    for em in emails:
        sig = classify_email_priority(em)
        scored_emails.append({**em, "urgency": sig.urgency, "score": sig.score, "reason": sig.reason})
    scored_emails.sort(key=lambda x: x["score"], reverse=True)

    approvals = approval_service.list_pending_approvals(db, user_id)

    return {
        "status": status,
        "events_24h": events,
        "calendar_conflicts": conflicts,
        "tasks_overdue": overdue_tasks,
        "tasks_due_soon": due_soon_tasks,
        "emails_scored": scored_emails,
        "pending_approvals": approvals,
    }


def _priority_lines(card: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    overdue = card.get("tasks_overdue", [])
    if overdue:
        lines.append(f"• {len(overdue)} tarefa(s) vencida(s) exigem ação imediata.")

    events = card.get("events_24h", [])
    if events:
        first = events[0]
        when = (first.get("start") or "")[:16].replace("T", " ")
        lines.append(f"• Próximo compromisso: {first.get('title', '?')} ({when}).")

    urgent_emails = [e for e in card.get("emails_scored", []) if e.get("urgency") in {"urgente", "importante"}]
    if urgent_emails:
        lines.append(f"• {len(urgent_emails)} e-mail(s) com prioridade alta.")

    if not lines:
        lines.append("• Dia estável: sem urgências críticas detectadas agora.")
    return lines


def _risk_lines(card: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    conflicts = card.get("calendar_conflicts", [])
    if conflicts:
        lines.append(f"• {len(conflicts)} conflito(s) de agenda detectado(s).")

    approvals = card.get("pending_approvals", [])
    if approvals:
        lines.append(f"• {len(approvals)} aprovação(ões) pendente(s).")

    noise_ratio = 0
    emails = card.get("emails_scored", [])
    if emails:
        noise = len([e for e in emails if e.get("urgency") == "ruído"])
        noise_ratio = int((noise / max(1, len(emails))) * 100)
    if noise_ratio >= 50:
        lines.append("• Caixa de entrada com alto ruído: priorizar filtragem.")

    if not lines:
        lines.append("• Sem riscos relevantes no momento.")
    return lines


def suggest_next_actions(card: dict[str, Any], limit: int = 3) -> list[str]:
    actions: list[str] = []
    for t in card.get("tasks_overdue", [])[:2]:
        actions.append(f"Resolver tarefa vencida: {t.get('title', '?')}")
    for c in card.get("calendar_conflicts", [])[:1]:
        actions.append(f"Ajustar conflito entre {c.get('a', '?')} e {c.get('b', '?')}")
    for em in card.get("emails_scored", []):
        if em.get("urgency") in {"urgente", "importante"}:
            actions.append(f"Responder e-mail prioritário: {em.get('subject', '(sem assunto)')}")
            break
    if not actions:
        actions.append("Executar foco profundo em 1 tarefa principal por 45 minutos")
    return actions[:limit]


def compose_executive_message(title: str, card: dict[str, Any], shortcuts: list[str] | None = None) -> str:
    lines = [f"🧭 *{title}*", ""]
    lines.append("*Prioridades*")
    lines.extend(_priority_lines(card))
    lines.append("")
    lines.append("*Riscos*")
    lines.extend(_risk_lines(card))
    lines.append("")
    lines.append("*Próximas ações*")
    for i, action in enumerate(suggest_next_actions(card), 1):
        lines.append(f"{i}. {action}")
    lines.append("")
    lines.append("*Atalhos*")
    for cmd in (shortcuts or ["/focus", "/briefingnow", "/approvals"]):
        lines.append(f"• {cmd}")
    return "\n".join(lines)


def compose_focus_message(card: dict[str, Any]) -> str:
    actions = suggest_next_actions(card, limit=3)
    lines = ["🎯 *Top 3 focos agora*"]
    for i, action in enumerate(actions, 1):
        lines.append(f"{i}. {action}")
    return "\n".join(lines)


def ensure_actionable_tail(reply: str, card: dict[str, Any]) -> str:
    if "Próximas ações" in reply or "*Próximas ações*" in reply:
        return reply
    actions = suggest_next_actions(card, limit=2)
    tail = "\n\n*Próximas ações*\n"
    tail += "\n".join(f"{i}. {a}" for i, a in enumerate(actions, 1))
    tail += "\n\n*Atalhos*\n• /focus\n• /myday"
    return reply.rstrip() + tail
