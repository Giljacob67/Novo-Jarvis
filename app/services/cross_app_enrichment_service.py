"""cross_app_enrichment_service.py — Contextual pre-meeting and event enrichment.

For a given calendar event or search query, this service:
1. Searches memories mentioning participants / title keywords
2. Fetches related Gmail threads (from or to attendees, about the subject)
3. Fetches relevant Drive files (title keyword search)
4. Fetches related Tasks (open tasks about the same subject)
5. Calls gpt-4o-mini to compose a structured pre-meeting briefing in PT-BR

The result is a text block Jarvis can send proactively 30 min before a meeting
or on demand via /enrichevent <title>.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

# ─── helpers ──────────────────────────────────────────────────────────────────


def _keywords_from_title(title: str, min_len: int = 3) -> list[str]:
    """Extract significant words from an event title for searching."""
    stop = {
        "com", "para", "sobre", "de", "do", "da", "dos", "das", "em", "no",
        "na", "nos", "nas", "e", "ou", "a", "o", "um", "uma", "the", "and",
        "for", "with", "of", "in", "on", "at", "by", "to",
    }
    words = re.findall(r"[a-záéíóúàâêôãõçüñA-Z0-9]+", title)
    return [w.lower() for w in words if len(w) >= min_len and w.lower() not in stop]


def _extract_attendee_names(event: dict) -> list[str]:
    """Extract participant display names from a calendar event dict."""
    attendees = event.get("attendees") or []
    names: list[str] = []
    for a in attendees:
        if isinstance(a, dict):
            name = (a.get("displayName") or a.get("email") or "").split("@")[0]
        else:
            name = str(a).split("@")[0]
        clean = name.strip()
        if clean:
            names.append(clean)
    return names


# ─── context gathering ─────────────────────────────────────────────────────────


async def _gather_relevant_memories(
    db: Session, user_id: str, keywords: list[str], limit: int = 6
) -> list[str]:
    try:
        from app.services.memory_service import list_memories
        all_memories = list_memories(db, user_id, limit=60)
        matched: list[str] = []
        for m in all_memories:
            content_lower = m.content.lower()
            if any(kw in content_lower for kw in keywords):
                matched.append(m.content)
            if len(matched) >= limit:
                break
        return matched
    except Exception as exc:
        logger.debug("Memory gather failed: %s", exc)
        return []


async def _gather_related_emails(
    db: Session, user_id: str, keywords: list[str], attendees: list[str], limit: int = 4
) -> list[dict]:
    try:
        from app.services import google_oauth_service, google_gmail_service
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected") or not status.get("gmail_enabled"):
            return []

        # Build Gmail search query: top 2 keywords OR attendee names
        kw_part = " OR ".join(f'"{kw}"' for kw in keywords[:2]) if keywords else ""
        att_part = " OR ".join(f'from:{a} OR to:{a}' for a in attendees[:2]) if attendees else ""
        if kw_part and att_part:
            query = f"({kw_part}) OR ({att_part})"
        elif kw_part:
            query = kw_part
        elif att_part:
            query = att_part
        else:
            return []

        result = await google_gmail_service.search_emails(
            db, user_id, query=query, max_results=limit
        )
        emails = result.get("messages", []) if "error" not in result else []
        return [
            {
                "subject": e.get("subject", ""),
                "from": e.get("from", ""),
                "snippet": (e.get("snippet") or "")[:200],
                "date": e.get("date", ""),
            }
            for e in emails
        ]
    except Exception as exc:
        logger.debug("Email gather failed: %s", exc)
        return []


async def _gather_related_drive_files(
    db: Session, user_id: str, keywords: list[str], limit: int = 3
) -> list[dict]:
    try:
        from app.services import google_oauth_service, google_drive_service
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected") or not status.get("drive_enabled"):
            return []

        query_str = " ".join(keywords[:3])
        if not query_str:
            return []

        result = await google_drive_service.search_files(db, user_id, query=query_str, limit=limit)
        files = result if isinstance(result, list) else result.get("files", [])
        return [
            {
                "name": f.get("name", ""),
                "type": f.get("mimeType", "").split(".")[-1],
                "modified": f.get("modifiedTime", "")[:10],
                "url": f.get("webViewLink", ""),
            }
            for f in files[:limit]
        ]
    except Exception as exc:
        logger.debug("Drive gather failed: %s", exc)
        return []


async def _gather_related_tasks(
    db: Session, user_id: str, keywords: list[str], limit: int = 4
) -> list[dict]:
    try:
        from app.services import google_oauth_service, google_tasks as google_tasks_service
        status = google_oauth_service.get_status(db, user_id)
        if not status.get("connected") or not status.get("tasks_enabled"):
            return []

        tasks = await google_tasks_service.list_tasks(db, user_id, limit=40)
        matched: list[dict] = []
        for t in tasks:
            if t.get("status") == "completed":
                continue
            title_lower = (t.get("title") or "").lower()
            notes_lower = (t.get("notes") or "").lower()
            blob = f"{title_lower} {notes_lower}"
            if any(kw in blob for kw in keywords):
                matched.append({
                    "title": t.get("title", ""),
                    "due": (t.get("due") or "")[:10],
                    "notes": (t.get("notes") or "")[:100],
                })
            if len(matched) >= limit:
                break
        return matched
    except Exception as exc:
        logger.debug("Tasks gather failed: %s", exc)
        return []


# ─── context assembly ──────────────────────────────────────────────────────────


async def enrich_event_context(
    db: Session,
    user_id: str,
    event_title: str,
    attendees: Optional[list[str]] = None,
) -> dict:
    """Gather all cross-app context for a given event title and attendees.

    Returns:
        {
            "keywords": list[str],
            "attendees": list[str],
            "memories": list[str],
            "emails": list[dict],
            "drive_files": list[dict],
            "tasks": list[dict],
        }
    """
    keywords = _keywords_from_title(event_title)
    attendees = attendees or []

    # Combine all query terms (keywords + attendee names)
    all_terms = list(dict.fromkeys(keywords + [a.lower() for a in attendees]))

    memories, emails, drive_files, tasks = await _gather_parallel(
        db, user_id, keywords=all_terms, attendees=attendees
    )

    return {
        "keywords": keywords,
        "attendees": attendees,
        "memories": memories,
        "emails": emails,
        "drive_files": drive_files,
        "tasks": tasks,
    }


async def _gather_parallel(
    db: Session, user_id: str, keywords: list[str], attendees: list[str]
) -> tuple[list, list, list, list]:
    """Run all four context gathers; failures are isolated."""
    import asyncio

    results = await asyncio.gather(
        _gather_relevant_memories(db, user_id, keywords),
        _gather_related_emails(db, user_id, keywords, attendees),
        _gather_related_drive_files(db, user_id, keywords),
        _gather_related_tasks(db, user_id, keywords),
        return_exceptions=True,
    )

    def safe(r, default):
        return r if isinstance(r, list) else default

    return (
        safe(results[0], []),
        safe(results[1], []),
        safe(results[2], []),
        safe(results[3], []),
    )


# ─── LLM briefing composition ─────────────────────────────────────────────────


async def compose_enriched_briefing(
    event_title: str,
    event_start: str,
    context: dict,
) -> str:
    """Use gpt-4o-mini to write a structured pre-meeting briefing in PT-BR."""
    try:
        from openai import AsyncOpenAI
        api_key = settings.openai_api_key
        if not api_key:
            return _fallback_briefing(event_title, event_start, context)

        client = AsyncOpenAI(api_key=api_key)

        # Build context block
        parts: list[str] = []

        if context.get("attendees"):
            parts.append("Participantes: " + ", ".join(context["attendees"]))

        if context.get("memories"):
            mem_lines = "\n".join(f"- {m}" for m in context["memories"])
            parts.append(f"O que sei sobre os envolvidos:\n{mem_lines}")

        if context.get("emails"):
            email_lines = "\n".join(
                f"- [{e['date'][:10]}] {e['from'][:40]} | {e['subject']} — {e['snippet'][:120]}"
                for e in context["emails"]
            )
            parts.append(f"E-mails relacionados:\n{email_lines}")

        if context.get("drive_files"):
            file_lines = "\n".join(
                f"- {f['name']} (editado: {f['modified']})"
                for f in context["drive_files"]
            )
            parts.append(f"Arquivos no Drive:\n{file_lines}")

        if context.get("tasks"):
            task_lines = "\n".join(
                f"- {t['title']}" + (f" [prazo: {t['due']}]" if t.get("due") else "")
                for t in context["tasks"]
            )
            parts.append(f"Tarefas abertas relacionadas:\n{task_lines}")

        if not parts:
            return _fallback_briefing(event_title, event_start, context)

        ctx_text = "\n\n".join(parts)

        prompt = (
            f"Você é Jarvis, assistente pessoal executivo.\n"
            f"O usuário tem uma reunião/evento em breve:\n"
            f"Título: {event_title}\n"
            f"Início: {event_start}\n\n"
            f"Contexto coletado de e-mails, Drive, Tarefas e memória:\n{ctx_text}\n\n"
            "Escreva um briefing executivo pré-reunião em português com:\n"
            "1. 📋 **Resumo do encontro** (1-2 frases)\n"
            "2. 🔑 **Pontos-chave** — o que é importante saber antes (bullets)\n"
            "3. 📧 **E-mails relevantes** — se houver, destacar o mais importante\n"
            "4. 📁 **Arquivos** — se houver, mencionar o mais pertinente\n"
            "5. ✅ **Ações pendentes** — tarefas abertas relacionadas, se existirem\n"
            "6. 💡 **Sugestões de pauta** — 2-3 tópicos para abordar\n\n"
            "Seja direto e acionável. Máximo 300 palavras. "
            "Omita seções sem conteúdo relevante."
        )

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
        )
        return (response.choices[0].message.content or "").strip() or _fallback_briefing(
            event_title, event_start, context
        )
    except Exception as exc:
        logger.warning("compose_enriched_briefing LLM failed: %s", exc)
        return _fallback_briefing(event_title, event_start, context)


def _fallback_briefing(event_title: str, event_start: str, context: dict) -> str:
    """Plain-text fallback briefing when LLM is unavailable."""
    lines = [f"📋 *Briefing pré-reunião: {event_title}*", f"🕐 {event_start}", ""]

    if context.get("attendees"):
        lines.append("👥 Participantes: " + ", ".join(context["attendees"]))

    if context.get("memories"):
        lines.append("\n🧠 *Notas relevantes:*")
        for m in context["memories"][:3]:
            lines.append(f"  • {m[:120]}")

    if context.get("emails"):
        lines.append("\n📧 *E-mails relacionados:*")
        for e in context["emails"][:2]:
            lines.append(f"  • {e['subject']} — {e['from'].split('<')[0].strip()[:30]}")

    if context.get("drive_files"):
        lines.append("\n📁 *Arquivos no Drive:*")
        for f in context["drive_files"][:2]:
            lines.append(f"  • {f['name']}")

    if context.get("tasks"):
        lines.append("\n✅ *Tarefas abertas relacionadas:*")
        for t in context["tasks"][:3]:
            lines.append(f"  • {t['title']}")

    if len(lines) <= 4:
        lines.append("_Nenhum contexto adicional encontrado nas suas ferramentas._")

    return "\n".join(lines)


# ─── full pipeline ─────────────────────────────────────────────────────────────


async def enrich_event_by_title(
    db: Session,
    user_id: str,
    event_title: str,
    event_start: str = "",
    attendees: Optional[list[str]] = None,
) -> str:
    """End-to-end: gather cross-app context → compose briefing → return text.

    Used by:
    - /enrichevent command (manual, on demand)
    - scheduler pre-meeting reminder (automatic, 30 min before)
    """
    if not settings.cross_app_enrichment_enabled:
        return f"📋 Enriquecimento cross-app está desativado. (`CROSS_APP_ENRICHMENT_ENABLED=true` para ativar)"

    context = await enrich_event_context(db, user_id, event_title, attendees=attendees)
    has_any = any(
        context.get(k) for k in ("memories", "emails", "drive_files", "tasks")
    )

    if not has_any:
        return (
            f"📋 *{event_title}*\n"
            f"🕐 {event_start or 'Horário não informado'}\n\n"
            "_Nenhum contexto encontrado em e-mails, Drive, tarefas ou memória._"
        )

    return await compose_enriched_briefing(event_title, event_start or "em breve", context)


async def find_and_enrich_event(
    db: Session,
    user_id: str,
    title_query: str,
) -> str:
    """Search today's calendar for a matching event, then enrich it.

    If no matching event found in calendar, uses the title_query directly.
    """
    event_title = title_query
    event_start = ""
    attendees: list[str] = []

    # Try to find matching event in today's calendar
    try:
        from app.services import google_oauth_service
        from app.services import google_calendar as google_calendar_service

        status = google_oauth_service.get_status(db, user_id)
        if status.get("connected"):
            events = await google_calendar_service.list_today_events(
                db, user_id, settings.default_timezone
            )
            query_lower = title_query.lower()
            for ev in (events or []):
                ev_title = (ev.get("title") or ev.get("summary") or "").lower()
                if query_lower in ev_title or ev_title in query_lower:
                    event_title = ev.get("title") or ev.get("summary") or title_query
                    event_start = ev.get("start", "")
                    attendees = _extract_attendee_names(ev)
                    break
    except Exception as exc:
        logger.debug("Calendar lookup for enrichment failed: %s", exc)

    return await enrich_event_by_title(
        db, user_id, event_title, event_start=event_start, attendees=attendees
    )
