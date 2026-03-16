import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.google_oauth_service import get_credentials
from app.utils.date_utils import today_bounds, week_bounds, get_tz

logger = logging.getLogger(__name__)


def _build_service(db: Session, user_id: str):
    creds = get_credentials(db, user_id)
    if creds is None:
        return None
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=creds)


async def list_today_events(db: Session, user_id: str, tz: str | None = None) -> list[dict[str, Any]]:
    service = _build_service(db, user_id)
    if service is None:
        return []

    start_dt, end_dt = today_bounds(tz)
    try:
        events_result = service.events().list(
            calendarId="primary",
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
            timeZone=tz or "America/Sao_Paulo",
        ).execute()
        return _format_events(events_result.get("items", []))
    except Exception:
        logger.exception("Failed to list today's events for user=%s", user_id)
        return []


async def list_upcoming_events(
    db: Session, user_id: str, days: int = 7, limit: int = 20, tz: str | None = None
) -> list[dict[str, Any]]:
    service = _build_service(db, user_id)
    if service is None:
        return []

    start_dt, end_dt = week_bounds(tz, days=days)
    try:
        events_result = service.events().list(
            calendarId="primary",
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=limit,
            timeZone=tz or "America/Sao_Paulo",
        ).execute()
        return _format_events(events_result.get("items", []))
    except Exception:
        logger.exception("Failed to list upcoming events for user=%s", user_id)
        return []


async def create_event(
    db: Session,
    user_id: str,
    title: str,
    start_dt: datetime,
    end_dt: datetime,
    tz: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    service = _build_service(db, user_id)
    if service is None:
        return {"error": "Google não conectado. Use /connectgoogle para conectar."}

    tz_name = tz or "America/Sao_Paulo"
    event_body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    try:
        created = service.events().insert(calendarId="primary", body=event_body).execute()
        logger.info("Created event id=%s for user=%s", created.get("id"), user_id)
        return {
            "id": created.get("id"),
            "title": created.get("summary"),
            "start": created.get("start", {}).get("dateTime", ""),
            "end": created.get("end", {}).get("dateTime", ""),
            "link": created.get("htmlLink", ""),
        }
    except Exception:
        logger.exception("Failed to create event for user=%s", user_id)
        return {"error": "Erro ao criar evento no Google Calendar."}


def _format_events(items: list[dict]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        start = item.get("start", {})
        end = item.get("end", {})
        result.append({
            "id": item.get("id", ""),
            "title": item.get("summary", "(sem título)"),
            "start": start.get("dateTime", start.get("date", "")),
            "end": end.get("dateTime", end.get("date", "")),
            "location": item.get("location", ""),
            "description": item.get("description", ""),
        })
    return result


async def update_event(
    db: Session,
    user_id: str,
    event_id: str,
    title: str | None = None,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    tz: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    service = _build_service(db, user_id)
    if service is None:
        return {"error": "Google não conectado."}

    try:
        # First get the existing event to patch it
        event = service.events().get(calendarId="primary", eventId=event_id).execute()
        
        if title:
            event["summary"] = title
        if start_dt:
            event["start"] = {"dateTime": start_dt.isoformat(), "timeZone": tz or "America/Sao_Paulo"}
        if end_dt:
            event["end"] = {"dateTime": end_dt.isoformat(), "timeZone": tz or "America/Sao_Paulo"}
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location

        updated = service.events().update(calendarId="primary", eventId=event_id, body=event).execute()
        return {
            "id": updated.get("id"),
            "title": updated.get("summary"),
            "status": "updated",
        }
    except Exception:
        logger.exception("Failed to update event id=%s for user=%s", event_id, user_id)
        return {"error": "Erro ao atualizar evento no Google Calendar."}


async def delete_event(db: Session, user_id: str, event_id: str) -> dict[str, Any]:
    service = _build_service(db, user_id)
    if service is None:
        return {"error": "Google não conectado."}

    try:
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return {"id": event_id, "status": "deleted"}
    except Exception:
        logger.exception("Failed to delete event id=%s for user=%s", event_id, user_id)
        return {"error": "Erro ao excluir evento no Google Calendar."}
