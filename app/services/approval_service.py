import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.pending_approval import PendingApproval
from app.models.action_log import ActionLog

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


VALID_ACTION_TYPES = [
    "send_email_draft",
    "create_followup_task",
    "update_task",
    "delete_task",
    "create_calendar_event_from_ai",
    "update_calendar_event",
    "delete_calendar_event",
    "send_proactive_followup_message",
    "browser_click",
    "browser_fill",
    "browser_submit_form",
]


def _log_action(db: Session, event_type: str, status: str, details: dict) -> None:
    entry = ActionLog(
        event_type=event_type,
        status=status,
        details_json=json.dumps(details, ensure_ascii=False),
    )
    db.add(entry)
    db.commit()


def create_pending_approval(
    db: Session,
    user_id: str,
    action_type: str,
    title: str,
    summary: str,
    payload: dict | None = None,
    source: str = "system",
    idempotency_key: str | None = None,
    expires_in_hours: int = 48,
) -> PendingApproval | None:
    if not settings.approvals_enabled:
        logger.info("Approvals disabled, skipping creation for user=%s", user_id)
        return None

    if action_type not in VALID_ACTION_TYPES:
        logger.warning("Invalid action_type=%s for user=%s", action_type, user_id)
        return None

    if idempotency_key:
        existing = db.query(PendingApproval).filter(
            PendingApproval.idempotency_key == idempotency_key,
        ).first()
        if existing:
            logger.info("Approval idempotency_key=%s already exists, skipping", idempotency_key)
            return existing

    pending_count = db.query(PendingApproval).filter(
        PendingApproval.user_id == user_id,
        PendingApproval.status == "pending",
    ).count()
    if pending_count >= settings.max_pending_approvals:
        logger.warning("Max pending approvals (%d) reached for user=%s", settings.max_pending_approvals, user_id)
        return None

    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

    approval = PendingApproval(
        user_id=user_id,
        action_type=action_type,
        title=title,
        summary=summary,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
        status="pending",
        source=source,
        idempotency_key=idempotency_key,
        expires_at=expires_at,
    )
    try:
        db.add(approval)
        db.commit()
        db.refresh(approval)
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            return db.query(PendingApproval).filter(
                PendingApproval.idempotency_key == idempotency_key,
            ).first()
        return None

    _log_action(db, "approval_created", "success", {
        "approval_id": approval.id,
        "user_id": user_id,
        "action_type": action_type,
        "title": title,
    })
    logger.info("Created approval id=%d type=%s for user=%s", approval.id, action_type, user_id)
    return approval


def list_pending_approvals(db: Session, user_id: str) -> list[PendingApproval]:
    now = datetime.now(timezone.utc)
    expired = db.query(PendingApproval).filter(
        PendingApproval.user_id == user_id,
        PendingApproval.status == "pending",
        PendingApproval.expires_at != None,
        PendingApproval.expires_at < now,
    ).all()
    for a in expired:
        a.status = "expired"
    if expired:
        db.commit()

    return db.query(PendingApproval).filter(
        PendingApproval.user_id == user_id,
        PendingApproval.status == "pending",
    ).order_by(PendingApproval.created_at.desc()).all()


def approve_pending_approval(db: Session, user_id: str, approval_id: int) -> dict:
    approval = db.query(PendingApproval).filter(
        PendingApproval.id == approval_id,
        PendingApproval.user_id == user_id,
    ).first()
    if not approval:
        return {"error": "Aprovação não encontrada."}

    if approval.status == "approved":
        return {"status": "already_approved", "message": "Esta aprovação já foi aprovada."}
    if approval.status != "pending":
        return {"error": f"Esta aprovação está com status '{approval.status}' e não pode ser aprovada."}

    if approval.expires_at and _ensure_aware(approval.expires_at) < _utcnow():
        approval.status = "expired"
        db.commit()
        return {"error": "Esta aprovação expirou."}

    approval.status = "approved"
    db.commit()

    _log_action(db, "approval_approved", "success", {
        "approval_id": approval.id,
        "user_id": user_id,
        "action_type": approval.action_type,
    })
    return {"status": "approved", "approval": approval}


def reject_pending_approval(db: Session, user_id: str, approval_id: int) -> dict:
    approval = db.query(PendingApproval).filter(
        PendingApproval.id == approval_id,
        PendingApproval.user_id == user_id,
    ).first()
    if not approval:
        return {"error": "Aprovação não encontrada."}

    if approval.status == "rejected":
        return {"status": "already_rejected", "message": "Esta aprovação já foi rejeitada."}
    if approval.status != "pending":
        return {"error": f"Esta aprovação está com status '{approval.status}' e não pode ser rejeitada."}

    approval.status = "rejected"
    db.commit()

    _log_action(db, "approval_rejected", "success", {
        "approval_id": approval.id,
        "user_id": user_id,
        "action_type": approval.action_type,
    })
    return {"status": "rejected", "approval": approval}


async def execute_approved_action(db: Session, user_id: str, approval_id: int) -> dict:
    approval = db.query(PendingApproval).filter(
        PendingApproval.id == approval_id,
        PendingApproval.user_id == user_id,
    ).first()
    if not approval:
        return {"error": "Aprovação não encontrada."}

    if approval.executed_at is not None:
        return {"status": "already_executed", "message": "Esta ação já foi executada."}

    if approval.status != "approved":
        return {"error": f"Aprovação com status '{approval.status}'. Precisa ser aprovada primeiro."}

    payload = json.loads(approval.payload_json) if approval.payload_json else {}

    result = await _dispatch_action(db, user_id, approval.action_type, payload)

    if "error" in result:
        approval.status = "execution_failed"
        db.commit()
        _log_action(db, "approval_execution_failed", "error", {
            "approval_id": approval.id,
            "user_id": user_id,
            "action_type": approval.action_type,
            "error": str(result["error"])[:500],
        })
        return {"status": "execution_failed", "error": result["error"]}

    approval.status = "executed"
    approval.executed_at = _utcnow()
    db.commit()

    _log_action(db, "approval_executed", "success", {
        "approval_id": approval.id,
        "user_id": user_id,
        "action_type": approval.action_type,
        "result": str(result)[:500],
    })
    return {"status": "executed", "result": result}


async def _dispatch_action(db: Session, user_id: str, action_type: str, payload: dict) -> dict:
    if action_type == "send_email_draft":
        from app.services import google_gmail_service
        draft_id = payload.get("draft_id", "")
        if draft_id:
            return await google_gmail_service.send_draft(db, user_id, draft_id=draft_id)
        to = payload.get("to", "")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        if to and subject and body:
            draft_result = await google_gmail_service.create_draft(db, user_id, to=to, subject=subject, body=body)
            if "error" in draft_result:
                return draft_result
            created_id = draft_result.get("draft_id", "")
            return await google_gmail_service.send_draft(db, user_id, draft_id=created_id)
        return {"error": "Payload incompleto para envio de e-mail."}

    if action_type == "create_followup_task":
        from app.services import google_tasks as google_tasks_service
        title = payload.get("title", "Follow-up")
        notes = payload.get("notes")
        due = payload.get("due")
        return await google_tasks_service.create_task(db, user_id, title, notes=notes, due=due)

    if action_type == "update_task":
        from app.services import google_tasks as google_tasks_service
        task_id = payload.get("task_id", "")
        if not task_id:
            return {"error": "Payload incompleto para atualização de tarefa (task_id)."}
        return await google_tasks_service.update_task(
            db,
            user_id,
            task_id=task_id,
            title=payload.get("title"),
            notes=payload.get("notes"),
            due=payload.get("due"),
        )

    if action_type == "delete_task":
        from app.services import google_tasks as google_tasks_service
        task_id = payload.get("task_id", "")
        if not task_id:
            return {"error": "Payload incompleto para exclusão de tarefa (task_id)."}
        return await google_tasks_service.delete_task(db, user_id, task_id=task_id)

    if action_type == "create_calendar_event_from_ai":
        from app.services import google_calendar as google_calendar_service
        from app.utils.date_utils import parse_datetime_local
        title = payload.get("title", "")
        tz = payload.get("timezone", settings.default_timezone)
        try:
            start_dt = parse_datetime_local(payload.get("start_time", ""), tz)
            end_dt = parse_datetime_local(payload.get("end_time", ""), tz)
        except ValueError as e:
            return {"error": str(e)}
        return await google_calendar_service.create_event(
            db, user_id, title, start_dt, end_dt, tz=tz,
            description=payload.get("description"),
            location=payload.get("location"),
        )

    if action_type == "update_calendar_event":
        from app.services import google_calendar as google_calendar_service
        from app.utils.date_utils import parse_datetime_local

        event_id = payload.get("event_id", "")
        if not event_id:
            return {"error": "Payload incompleto para atualização de evento (event_id)."}

        tz = payload.get("timezone", settings.default_timezone)
        start_time = payload.get("start_time")
        end_time = payload.get("end_time")
        start_dt = None
        end_dt = None
        try:
            if start_time:
                start_dt = parse_datetime_local(start_time, tz)
            if end_time:
                end_dt = parse_datetime_local(end_time, tz)
        except ValueError as e:
            return {"error": str(e)}

        return await google_calendar_service.update_event(
            db,
            user_id,
            event_id=event_id,
            title=payload.get("title"),
            start_dt=start_dt,
            end_dt=end_dt,
            tz=tz,
            description=payload.get("description"),
            location=payload.get("location"),
        )

    if action_type == "delete_calendar_event":
        from app.services import google_calendar as google_calendar_service
        event_id = payload.get("event_id", "")
        if not event_id:
            return {"error": "Payload incompleto para exclusão de evento (event_id)."}
        return await google_calendar_service.delete_event(db, user_id, event_id=event_id)

    if action_type == "send_proactive_followup_message":
        return {"status": "sent", "message": payload.get("message", "")}

    if action_type in ("browser_click", "browser_fill", "browser_submit_form"):
        from app.services import browser_service
        session_id = payload.get("session_id", "")
        approval_id_val = payload.get("approval_id", 0)
        return await browser_service.approve_and_execute_browser_action(
            db, user_id, session_id, approval_id_val, payload
        )

    return {"error": f"Tipo de ação '{action_type}' não suportado."}
