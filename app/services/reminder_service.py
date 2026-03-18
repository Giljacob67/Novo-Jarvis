"""reminder_service.py — Natural language reminder parsing and management.

Supports Brazilian Portuguese time expressions:
  "daqui 2h", "em 30 minutos", "amanhã às 10h", "hoje às 15:30",
  "na segunda às 9h", "semana que vem", "daqui 3 dias", etc.

Falls back to gpt-4o-mini for complex expressions that regex can't handle.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.reminder import Reminder

logger = logging.getLogger(__name__)

# ── Time unit maps ────────────────────────────────────────────────────────────

_UNIT_TO_SECONDS: dict[str, int] = {
    "segundo": 1, "segundos": 1,
    "minuto": 60, "minutos": 60, "min": 60, "mins": 60,
    "hora": 3600, "horas": 3600, "h": 3600,
    "dia": 86400, "dias": 86400,
    "semana": 604800, "semanas": 604800,
}

_WEEKDAYS_PT: dict[str, int] = {
    "segunda": 0, "segunda-feira": 0,
    "terça": 1, "terca": 1, "terça-feira": 1,
    "quarta": 2, "quarta-feira": 2,
    "quinta": 3, "quinta-feira": 3,
    "sexta": 4, "sexta-feira": 4,
    "sábado": 5, "sabado": 5,
    "domingo": 6,
}

_TRIGGER_VERBS = re.compile(
    r"\b(me\s+lembra|me\s+avisa|lembra\s+de|avisa\s+quando|me\s+notifica|"
    r"lembra|avisa|notifica|remindme|remind)\b",
    re.IGNORECASE,
)

_RELATIVE_RE = re.compile(
    r"(?:daqui|em|depois\s+de)\s+(\d+)\s*"
    r"(segundo[s]?|minuto[s]?|min[s]?|hora[s]?|h|dia[s]?|semana[s]?)",
    re.IGNORECASE,
)

_CLOCK_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*h(?:oras?)?\b", re.IGNORECASE)
_CLOCK24_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _now_tz() -> datetime:
    import zoneinfo
    tz = zoneinfo.ZoneInfo(settings.default_timezone)
    return datetime.now(tz)


def _extract_clock(text: str) -> Optional[tuple[int, int]]:
    """Extract (hour, minute) from '14h', '14:30', '9h30' patterns."""
    m = _CLOCK_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2) or 0)
    m = _CLOCK24_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_reminder_datetime(text: str) -> Optional[datetime]:
    """Parse a Brazilian Portuguese time expression into a UTC-aware datetime.

    Returns None if no recognisable time expression found.
    """
    text_lower = text.lower().strip()
    now = _now_tz()

    # 1) "daqui X unidade" / "em X unidade"
    m = _RELATIVE_RE.search(text_lower)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower().rstrip("s")  # normalise plural
        seconds = _UNIT_TO_SECONDS.get(unit, _UNIT_TO_SECONDS.get(unit + "s", 0))
        if seconds:
            return now + timedelta(seconds=amount * seconds)

    # 2) "amanhã às HH:MM"
    if "amanhã" in text_lower or "amanha" in text_lower:
        base = now + timedelta(days=1)
        clock = _extract_clock(text_lower)
        if clock:
            return base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return base.replace(hour=9, minute=0, second=0, microsecond=0)

    # 3) "hoje às HH:MM" or just "às HH:MM"
    if "hoje" in text_lower or re.search(r"\bàs?\b", text_lower):
        clock = _extract_clock(text_lower)
        if clock:
            candidate = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

    # 4) "na segunda/terça/... às HH:MM"
    for day_name, weekday_num in _WEEKDAYS_PT.items():
        if day_name in text_lower:
            days_ahead = (weekday_num - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # next occurrence
            base = now + timedelta(days=days_ahead)
            clock = _extract_clock(text_lower)
            if clock:
                return base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            return base.replace(hour=9, minute=0, second=0, microsecond=0)

    # 5) "semana que vem"
    if "semana que vem" in text_lower or "próxima semana" in text_lower:
        base = now + timedelta(weeks=1)
        clock = _extract_clock(text_lower)
        if clock:
            return base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return base.replace(hour=9, minute=0, second=0, microsecond=0)

    return None


async def parse_reminder_with_llm(text: str) -> Optional[dict]:
    """Fallback: use gpt-4o-mini to extract {message, trigger_at ISO} from free text."""
    try:
        from openai import AsyncOpenAI
        api_key = settings.openai_api_key
        if not api_key:
            return None
        client = AsyncOpenAI(api_key=api_key)
        now_str = _now_tz().strftime("%Y-%m-%d %H:%M %Z")
        prompt = (
            f"Agora são {now_str}.\n"
            "O usuário disse: \"" + text + "\"\n\n"
            "Extraia:\n"
            "1. O que ele quer ser lembrado (campo 'message', em português, max 100 chars)\n"
            "2. Quando (campo 'trigger_at', formato ISO 8601 com timezone, ex: 2024-01-15T14:00:00-03:00)\n\n"
            "Responda APENAS JSON: {\"message\": \"...\", \"trigger_at\": \"...\"}\n"
            "Se não houver informação de tempo, use trigger_at = null."
        )
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "{}").strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("parse_reminder_with_llm failed: %s", exc)
        return None


def _strip_time_expression(text: str) -> str:
    """Remove the time expression from the text, leaving just the reminder message."""
    patterns = [
        _RELATIVE_RE,
        re.compile(r"(amanhã|amanha)\s*(às?\s*\d{1,2}(?::\d{2})?\s*h(?:oras?)?)?", re.I),
        re.compile(r"(hoje\s*)?(às?\s*\d{1,2}(?::\d{2})?\s*h(?:oras?)?)", re.I),
        re.compile(r"(na\s+)?(segunda|terça|quarta|quinta|sexta|sábado|domingo)(\-feira)?(\s+às?\s*\d{1,2}(?::\d{2})?\s*h(?:oras?)?)?", re.I),
        re.compile(r"semana que vem|próxima semana", re.I),
        _TRIGGER_VERBS,
        re.compile(r"\b(daqui|em|depois\s+de|às?)\b", re.I),
    ]
    result = text
    for pat in patterns:
        result = pat.sub("", result)
    # clean up double spaces, leading/trailing
    result = re.sub(r"\s{2,}", " ", result).strip(" ,.-:")
    return result or text


async def parse_reminder_intent(text: str) -> Optional[dict]:
    """Detect and parse a reminder intent from free text.

    Returns:
        {"message": str, "trigger_at": datetime} or None if not a reminder request.
    """
    if not _TRIGGER_VERBS.search(text):
        return None

    # Try regex parser first (fast, no API call)
    trigger_dt = parse_reminder_datetime(text)
    message = _strip_time_expression(text)

    if trigger_dt is None:
        # Fallback to LLM
        llm_result = await parse_reminder_with_llm(text)
        if not llm_result:
            return None
        trigger_iso = llm_result.get("trigger_at")
        if not trigger_iso:
            return None
        try:
            from dateutil.parser import parse as dt_parse
            trigger_dt = dt_parse(trigger_iso)
        except Exception:
            return None
        message = llm_result.get("message") or message

    if not message:
        message = text[:100]

    return {"message": message, "trigger_at": trigger_dt}


# ── DB operations ─────────────────────────────────────────────────────────────

def create_reminder(
    db: Session,
    user_id: str,
    message: str,
    trigger_at: datetime,
    raw_input: Optional[str] = None,
) -> Reminder:
    reminder = Reminder(
        user_id=user_id,
        message=message,
        trigger_at=trigger_at,
        raw_input=raw_input,
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    logger.info(
        "Reminder created id=%d user=%s trigger=%s msg=%s",
        reminder.id, user_id, trigger_at.isoformat(), message[:60],
    )
    return reminder


def get_due_reminders(db: Session) -> list[Reminder]:
    """Return all unsent reminders whose trigger_at has passed."""
    import zoneinfo
    now_utc = datetime.now(timezone.utc)
    # trigger_at is stored without tz on some DBs — compare naively
    now_naive = datetime.utcnow()
    return (
        db.query(Reminder)
        .filter(
            Reminder.is_sent == False,  # noqa: E712
            Reminder.trigger_at <= now_naive,
        )
        .order_by(Reminder.trigger_at.asc())
        .limit(50)
        .all()
    )


def mark_sent(db: Session, reminder_id: int) -> None:
    reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
    if reminder:
        reminder.is_sent = True
        db.commit()


def list_pending_reminders(db: Session, user_id: str) -> list[Reminder]:
    return (
        db.query(Reminder)
        .filter(Reminder.user_id == user_id, Reminder.is_sent == False)  # noqa: E712
        .order_by(Reminder.trigger_at.asc())
        .limit(10)
        .all()
    )


def format_trigger_relative(trigger_at: datetime) -> str:
    """Format trigger_at as a human-readable relative time string in PT."""
    now = datetime.utcnow()
    delta = trigger_at - now
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "agora"
    if total_seconds < 60:
        return f"em {total_seconds} segundo{'s' if total_seconds != 1 else ''}"
    if total_seconds < 3600:
        mins = total_seconds // 60
        return f"em {mins} minuto{'s' if mins != 1 else ''}"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        mins = (total_seconds % 3600) // 60
        base = f"em {hours}h"
        return base + (f"{mins:02d}min" if mins else "")
    days = total_seconds // 86400
    return f"em {days} dia{'s' if days != 1 else ''}"
