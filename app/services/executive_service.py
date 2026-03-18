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
from app.services.memory_service import get_memories_by_context


@dataclass
class EmailSignal:
    urgency: str
    score: int
    reason: str


@dataclass
class BriefingItem:
    severity: str
    title: str
    detail: str
    action: str
    due_hint: str = ""
    source: str = ""


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

    urgent_kw = (
        "urgente", "prazo", "venc", "intima", "judicial", "falha", "error", "vpn", "aws", "security",
        "atraso", "suspens", "negativ", "bloque", "inadimpl", "confiss", "dívida", "divida",
    )
    noise_kw = ("newsletter", "promo", "oferta", "sale", "boletim", "trilhas várias", "unsubscribe", "descadastre")
    important_kw = ("reunião", "reuniao", "cliente", "follow-up", "followup", "proposta", "contrato", "assinatura")

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

    raw_events: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    emails: list[dict[str, Any]] = []

    if status.get("connected"):
        try:
            raw_events = await google_calendar_service.list_upcoming_events(
                db, user_id, days=5, limit=40, tz=settings.default_timezone
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
    followups = get_memories_by_context(db, user_id, ["followup", "decision", "project"], limit=8)

    return {
        "status": status,
        "events_24h": events,
        "events_upcoming": raw_events,
        "calendar_conflicts": conflicts,
        "tasks_overdue": overdue_tasks,
        "tasks_due_soon": due_soon_tasks,
        "emails_scored": scored_emails,
        "pending_approvals": approvals,
        "followups": followups,
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


def _severity_rank(severity: str) -> int:
    order = {"critical": 0, "high": 1, "moderate": 2}
    return order.get(severity, 3)


def _trim(text: str, max_len: int = 200) -> str:
    raw = (text or "").strip()
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 3].rstrip() + "..."


def _weekday_pt(dt: datetime) -> str:
    names = {
        0: "segunda-feira",
        1: "terça-feira",
        2: "quarta-feira",
        3: "quinta-feira",
        4: "sexta-feira",
        5: "sábado",
        6: "domingo",
    }
    return names.get(dt.weekday(), "dia útil")


def _task_to_item(task: dict[str, Any], today_iso: str) -> BriefingItem:
    title = task.get("title", "(tarefa sem título)")
    due = (task.get("due") or "")[:10]
    notes = _trim(task.get("notes") or "", 180)
    blob = f"{title} {notes}".lower()

    legal_kw = ("prazo", "process", "intima", "custa", "judicial")
    finance_kw = ("fatura", "débito", "debito", "parcela", "atraso", "pagamento", "boleto")

    severity = "moderate"
    if due and due <= today_iso:
        severity = "critical"
    if any(k in blob for k in legal_kw + finance_kw):
        if severity == "moderate":
            severity = "high"
        if due and due <= today_iso:
            severity = "critical"

    action = "Definir responsável e executar hoje."
    if any(k in blob for k in legal_kw):
        action = "Abrir o caso, validar prazo legal e montar plano de cumprimento."
    if any(k in blob for k in finance_kw):
        action = "Conferir valor e regularizar pendência financeira hoje."

    detail = notes or "Pendência registrada na sua lista de tarefas."
    due_hint = f"Vence em {due}" if due else ""
    if due and due < today_iso:
        due_hint = f"Vencida desde {due}"
    if due and due == today_iso:
        due_hint = f"Vence hoje ({due})"

    return BriefingItem(
        severity=severity,
        title=title,
        detail=detail,
        action=action,
        due_hint=due_hint,
        source="tasks",
    )


def _email_to_item(email: dict[str, Any]) -> BriefingItem:
    subject = email.get("subject", "(sem assunto)")
    sender = email.get("from", "remetente desconhecido")
    snippet = _trim(email.get("snippet") or "", 180)
    urgency = email.get("urgency", "informativo")
    reason = email.get("reason", "relevante para hoje")
    blob = f"{subject} {snippet} {sender}".lower()

    severity = "high" if urgency in {"urgente", "importante"} else "moderate"
    if any(k in blob for k in ("atraso", "suspens", "negativ", "intima", "prazo", "judicial")):
        severity = "critical"

    action = "Ler mensagem completa e responder/delegar."
    if "assinatura" in blob:
        action = "Abrir o documento e concluir a assinatura eletrônica."
    elif any(k in blob for k in ("atraso", "débito", "debito", "parcela", "fatura", "boleto")):
        action = "Validar valores e regularizar a pendência financeira."
    elif any(k in blob for k in ("intima", "process", "prazo")):
        action = "Ler o conteúdo jurídico e definir responsável com prazo."

    detail = f"{_trim(sender, 70)} — {reason}."
    if snippet:
        detail += f" {snippet}"
    return BriefingItem(
        severity=severity,
        title=subject,
        detail=detail,
        action=action,
        source="email",
    )


def _followup_to_item(content: str) -> BriefingItem:
    text = _trim(content, 220)
    return BriefingItem(
        severity="moderate",
        title="Follow-up pendente",
        detail=text,
        action="Fechar pendência ou reagendar com responsável.",
        source="memory",
    )


def build_briefing_items(card: dict[str, Any]) -> list[BriefingItem]:
    now = _now_local()
    today_iso = now.date().isoformat()
    items: list[BriefingItem] = []

    for task in card.get("tasks_overdue", [])[:4]:
        items.append(_task_to_item(task, today_iso))
    for task in card.get("tasks_due_soon", [])[:4]:
        items.append(_task_to_item(task, today_iso))

    for email in card.get("emails_scored", [])[:6]:
        if email.get("urgency") in {"urgente", "importante"}:
            items.append(_email_to_item(email))

    for c in card.get("calendar_conflicts", [])[:2]:
        items.append(
            BriefingItem(
                severity="high",
                title=f"Conflito de agenda: {c.get('a', '?')} x {c.get('b', '?')}",
                detail="Há sobreposição de compromissos no calendário.",
                action="Decidir prioridade e remarcar um dos eventos.",
                source="calendar",
            )
        )

    approvals = card.get("pending_approvals", [])
    if approvals:
        items.append(
            BriefingItem(
                severity="high",
                title=f"{len(approvals)} aprovação(ões) pendente(s)",
                detail="Ações críticas aguardam sua confirmação.",
                action="Revisar com /approvals e decidir agora.",
                source="approvals",
            )
        )

    for m in card.get("followups", [])[:2]:
        if getattr(m, "content", ""):
            items.append(_followup_to_item(m.content))

    items.sort(key=lambda i: _severity_rank(i.severity))
    dedup: list[BriefingItem] = []
    seen: set[str] = set()
    for item in items:
        key = item.title.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)
    return dedup[:10]


def _agenda_deadline_lines(card: dict[str, Any], limit: int = 6) -> list[str]:
    now = _now_local().date()
    lines: list[str] = []

    due_rows: list[tuple[str, str]] = []
    for task in card.get("tasks_overdue", []) + card.get("tasks_due_soon", []):
        due = (task.get("due") or "")[:10]
        if not due:
            continue
        due_rows.append((due, task.get("title", "(tarefa)")))

    for ev in card.get("events_upcoming", [])[:20]:
        start = (ev.get("start") or "")[:10]
        if not start:
            continue
        due_rows.append((start, f"Evento: {ev.get('title', '?')}"))

    due_rows.sort(key=lambda x: x[0])
    for date_str, title in due_rows:
        if len(lines) >= limit:
            break
        tag = "HOJE" if date_str == now.isoformat() else date_str
        lines.append(f"- {tag}: {_trim(title, 90)}")

    return lines


def compose_morning_briefing(card: dict[str, Any]) -> str:
    now = _now_local()
    items = build_briefing_items(card)
    critical = [i for i in items if i.severity == "critical"]
    high = [i for i in items if i.severity == "high"]
    moderate = [i for i in items if i.severity == "moderate"]

    lines: list[str] = [
        f"Bom dia. Briefing de hoje — {_weekday_pt(now)}, {now.strftime('%d/%m')}.",
        "",
    ]

    def _append_section(title: str, section_items: list[BriefingItem], max_items: int) -> None:
        if not section_items:
            return
        lines.append(f"*{title}:*")
        for idx, item in enumerate(section_items[:max_items], 1):
            lines.append(f"{idx}. {item.title}")
            lines.append(f"   {item.detail}")
            if item.due_hint:
                lines.append(f"   Prazo: {item.due_hint}")
            lines.append(f"   Ação: {item.action}")
        lines.append("")

    _append_section("CRÍTICO — ação imediata", critical, 3)
    _append_section("ATENÇÃO ALTA", high, 3)
    _append_section("MODERADO", moderate, 2)

    agenda_lines = _agenda_deadline_lines(card)
    if agenda_lines:
        lines.append("*PRAZOS E AGENDA (próximos dias):*")
        lines.extend(agenda_lines)
        lines.append("")

    lines.append("*Próximas ações*")
    for i, action in enumerate(suggest_next_actions(card, limit=3), 1):
        lines.append(f"{i}. {action}")
    lines.append("")
    lines.append("*Atalhos*")
    lines.append("• /focus")
    lines.append("• /checkin")
    lines.append("• /inboxsummary")
    lines.append("• /approvals")
    return "\n".join(lines)


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


# ── Enhanced on-demand briefing ───────────────────────────────────────────────

def _time_greeting() -> str:
    """Return contextual Portuguese greeting based on current hour."""
    hour = _now_local().hour
    if hour < 12:
        return "Bom dia"
    if hour < 18:
        return "Boa tarde"
    return "Boa noite"


def _kpi_header(card: dict[str, Any]) -> str:
    """Format a compact KPI summary line for the briefing header."""
    events_count = len(card.get("events_24h", []))
    urgent_emails = len([
        e for e in card.get("emails_scored", [])
        if e.get("urgency") in {"urgente", "importante"}
    ])
    overdue = len(card.get("tasks_overdue", []))
    due_soon = len(card.get("tasks_due_soon", []))
    conflicts = len(card.get("calendar_conflicts", []))
    approvals = len(card.get("pending_approvals", []))

    parts: list[str] = []
    if events_count:
        parts.append(f"🗓 {events_count} evento{'s' if events_count != 1 else ''} hoje")
    if urgent_emails:
        parts.append(f"📧 {urgent_emails} e-mail{'s' if urgent_emails != 1 else ''} urgente{'s' if urgent_emails != 1 else ''}")
    if overdue:
        parts.append(f"⚠️ {overdue} tarefa{'s' if overdue != 1 else ''} vencida{'s' if overdue != 1 else ''}")
    if due_soon:
        parts.append(f"📋 {due_soon} vence{'m' if due_soon != 1 else ''} hoje")
    if conflicts:
        parts.append(f"⚡ {conflicts} conflito{'s' if conflicts != 1 else ''} de agenda")
    if approvals:
        parts.append(f"✅ {approvals} aprovação{'ões' if approvals != 1 else ''} pendente{'s' if approvals != 1 else ''}")
    if not parts:
        parts.append("✅ Dia tranquilo — sem urgências")
    return " · ".join(parts)


async def generate_briefing_insight(card: dict[str, Any]) -> str:
    """Use gpt-4o-mini to generate a 2-sentence strategic insight from the context card."""
    try:
        from app.config import settings as _settings
        from openai import AsyncOpenAI
        api_key = _settings.openai_api_key
        if not api_key:
            return ""
        client = AsyncOpenAI(api_key=api_key)

        events = card.get("events_24h", [])
        overdue = card.get("tasks_overdue", [])
        urgent_emails = [e for e in card.get("emails_scored", []) if e.get("urgency") in {"urgente", "importante"}]
        conflicts = card.get("calendar_conflicts", [])

        ctx_parts: list[str] = []
        if events:
            ctx_parts.append("Eventos próximas 24h: " + ", ".join(e.get("title", "?") for e in events[:5]))
        if overdue:
            ctx_parts.append("Tarefas vencidas: " + ", ".join(t.get("title", "?") for t in overdue[:3]))
        if urgent_emails:
            ctx_parts.append("E-mails urgentes: " + ", ".join(e.get("subject", "?") for e in urgent_emails[:3]))
        if conflicts:
            ctx_parts.append(f"{len(conflicts)} conflito(s) de agenda detectado(s)")

        if not ctx_parts:
            return ""

        context = "\n".join(ctx_parts)
        prompt = (
            "Você é Jarvis, assistente pessoal executivo.\n"
            "Dado este contexto do dia do usuário:\n"
            f"{context}\n\n"
            "Escreva exatamente 2 frases em português:\n"
            "1. Uma observação estratégica sobre o dia (risco, oportunidade ou padrão que o usuário talvez não perceba)\n"
            "2. Uma recomendação concreta de ação prioritária\n"
            "Seja direto e específico. Máximo 60 palavras total. Sem introduções como 'Com base em' ou 'Olhando para'."
        )
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.4,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:
        return ""


async def compose_briefing_on_demand(card: dict[str, Any]) -> str:
    """Compose a rich, time-aware on-demand executive briefing with AI insight."""
    now = _now_local()
    greeting = _time_greeting()
    weekday = _weekday_pt(now)
    date_str = now.strftime("%d/%m · %H:%M")

    kpi = _kpi_header(card)
    insight = await generate_briefing_insight(card)

    items = build_briefing_items(card)
    critical = [i for i in items if i.severity == "critical"]
    high = [i for i in items if i.severity == "high"]
    moderate = [i for i in items if i.severity == "moderate"]

    lines: list[str] = [
        f"🤖 *{greeting}. Briefing — {weekday}, {date_str}*",
        "",
        f"_{kpi}_",
        "",
    ]

    if insight:
        lines += [f"💡 {insight}", ""]

    def _append_section(title: str, section_items: list[BriefingItem], max_items: int) -> None:
        if not section_items:
            return
        lines.append(f"*{title}*")
        for item in section_items[:max_items]:
            lines.append(f"• {item.title}")
            lines.append(f"  ↳ {item.action}")
        lines.append("")

    _append_section("🔴 Crítico", critical, 3)
    _append_section("🟠 Alta prioridade", high, 3)
    _append_section("🟡 Atenção", moderate, 2)

    agenda_lines = _agenda_deadline_lines(card)
    if agenda_lines:
        lines.append("*📅 Próximos prazos e eventos*")
        lines.extend(agenda_lines[:5])
        lines.append("")

    actions = suggest_next_actions(card, limit=3)
    if actions:
        lines.append("*🎯 Top ações agora*")
        for i, action in enumerate(actions, 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    lines += [
        "*Atalhos*",
        "• /focus · /myday · /inboxsummary · /approvals",
    ]
    return "\n".join(lines)
