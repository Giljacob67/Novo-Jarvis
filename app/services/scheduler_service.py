import asyncio
import logging
from datetime import datetime, time, timezone

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models.routine_execution_log import RoutineExecutionLog

logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None
_running = False


def _now_in_tz() -> datetime:
    import zoneinfo
    tz = zoneinfo.ZoneInfo(settings.default_timezone)
    return datetime.now(tz)


def _try_claim_run(routine_type: str, run_key: str) -> bool:
    db = SessionLocal()
    try:
        entry = RoutineExecutionLog(
            routine_type=routine_type,
            run_key=run_key,
            status="completed",
        )
        db.add(entry)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def _time_matches(current: time, target_str: str, tolerance_minutes: int = 2) -> bool:
    target = time.fromisoformat(target_str)
    current_minutes = current.hour * 60 + current.minute
    target_minutes = target.hour * 60 + target.minute
    return abs(current_minutes - target_minutes) <= tolerance_minutes


async def _run_scheduler_loop() -> None:
    global _running
    _running = True
    logger.info("Scheduler started (interval=%dm)", settings.reminder_check_interval_minutes)

    while _running:
        try:
            await _check_routines()
        except Exception:
            logger.exception("Scheduler loop error")

        await asyncio.sleep(settings.reminder_check_interval_minutes * 60)


def _is_routine_enabled_for_user(user_id: str, routine_type: str, global_default: bool) -> bool:
    from app.models.routine_config import RoutineConfig
    db = SessionLocal()
    try:
        config = db.query(RoutineConfig).filter(
            RoutineConfig.user_id == user_id,
            RoutineConfig.routine_type == routine_type,
        ).first()
        if config is not None:
            return config.is_enabled
        return global_default
    finally:
        db.close()


async def _check_routines() -> None:
    if not settings.proactive_features_enabled:
        return

    now = _now_in_tz()
    current_time = now.time()
    today_str = now.strftime("%Y-%m-%d")
    user_id = settings.telegram_allowed_user_id
    if not user_id:
        return

    if settings.morning_briefing_enabled and _time_matches(current_time, settings.morning_briefing_time):
        if _is_routine_enabled_for_user(user_id, "morning", True):
            run_key = f"briefing_{today_str}"
            if _try_claim_run("morning_briefing", run_key):
                logger.info("Running morning briefing for date=%s", today_str)
                await _send_briefing(user_id)

    if settings.evening_review_enabled and _time_matches(current_time, settings.evening_review_time):
        if _is_routine_enabled_for_user(user_id, "evening", True):
            run_key = f"review_{today_str}"
            if _try_claim_run("evening_review", run_key):
                logger.info("Running evening review for date=%s", today_str)
                await _send_review(user_id)

    if settings.midday_checkin_enabled and _time_matches(current_time, settings.midday_checkin_time):
        if _is_routine_enabled_for_user(user_id, "midday", True):
            run_key = f"checkin_{today_str}"
            if _try_claim_run("midday_checkin", run_key):
                logger.info("Running midday check-in for date=%s", today_str)
                await _send_checkin(user_id)

    if _is_routine_enabled_for_user(user_id, "reminders", True):
        reminder_key = f"reminders_{today_str}_{now.strftime('%H')}"
        if _try_claim_run("reminder_check", reminder_key):
            await _check_reminders(user_id)

    hour_str = now.strftime("%Y-%m-%d_%H")
    browser_cleanup_key = f"browser_cleanup_{hour_str}"
    if _try_claim_run("browser_cleanup", browser_cleanup_key):
        await _cleanup_browser_sessions()


async def _send_briefing(user_id: str) -> None:
    db = SessionLocal()
    try:
        from app.services.proactive_service import generate_morning_briefing, send_proactive_message
        briefing = await generate_morning_briefing(db, user_id)
        await send_proactive_message(
            db,
            user_id,
            briefing,
            subject="morning_briefing",
            proactive_category="morning_briefing",
            trigger_type="routine",
        )
    except Exception:
        logger.exception("Failed to send morning briefing")
    finally:
        db.close()


async def _send_review(user_id: str) -> None:
    db = SessionLocal()
    try:
        from app.services.proactive_service import generate_evening_review, send_proactive_message
        review = await generate_evening_review(db, user_id)
        await send_proactive_message(
            db,
            user_id,
            review,
            subject="evening_review",
            proactive_category="evening_review",
            trigger_type="routine",
        )
    except Exception:
        logger.exception("Failed to send evening review")
    finally:
        db.close()


async def _send_checkin(user_id: str) -> None:
    db = SessionLocal()
    try:
        from app.services.proactive_service import generate_midday_checkin, send_proactive_message
        checkin = await generate_midday_checkin(db, user_id)
        await send_proactive_message(
            db,
            user_id,
            checkin,
            subject="midday_checkin",
            proactive_category="midday_checkin",
            trigger_type="routine",
        )
    except Exception:
        logger.exception("Failed to send midday check-in")
    finally:
        db.close()


async def _check_reminders(user_id: str) -> None:
    db = SessionLocal()
    try:
        from app.services.proactive_service import (
            check_upcoming_events,
            check_due_tasks,
            check_stale_followups,
            detect_event_driven_alerts,
            generate_pre_meeting_briefing,
            generate_smart_email_alert,
            send_proactive_message,
        )

        # ── Pre-meeting briefings (context-enriched via LLM) ──────────────
        upcoming = await check_upcoming_events(db, user_id)
        for ev in upcoming:
            title = ev.get("title", "?")
            msg = await generate_pre_meeting_briefing(db, user_id, ev)
            await send_proactive_message(
                db,
                user_id,
                msg,
                subject=f"event_reminder_{title}",
                proactive_category="event_reminder",
                trigger_type="routine",
            )

        # ── Due tasks digest ───────────────────────────────────────────────
        due_tasks = await check_due_tasks(db, user_id)
        if due_tasks:
            lines = [f"📋 *{len(due_tasks)} tarefa(s) vencendo hoje:*"]
            for t in due_tasks[:5]:
                lines.append(f"  • {t['title']}")
            await send_proactive_message(
                db,
                user_id,
                "\n".join(lines),
                subject="due_tasks_reminder",
                proactive_category="deadline",
                trigger_type="routine",
            )

        # ── Event-driven alerts (email critical, conflicts, approvals) ─────
        event_alerts = await detect_event_driven_alerts(db, user_id)
        for alert in event_alerts[:8]:
            # Enrich critical-email alerts with AI summary + suggested action
            if alert.get("proactive_category") == "email_critical":
                email_data = alert.get("email_data") or {
                    "subject": alert.get("subject", ""),
                    "snippet": "",
                }
                enriched_msg = await generate_smart_email_alert(email_data)
                alert = {**alert, "message": enriched_msg}
            await send_proactive_message(
                db,
                user_id,
                alert.get("message", ""),
                subject=alert.get("subject", "event_alert"),
                proactive_category=alert.get("proactive_category", "general"),
                trigger_type=alert.get("trigger_type", "event"),
            )

        # ── Stale follow-up nudges ─────────────────────────────────────────
        if settings.stale_followup_nudge_enabled:
            stale = check_stale_followups(db, user_id)
            if stale:
                lines = ["📌 *Follow-ups pendentes — ainda não resolvidos:*"]
                for m in stale:
                    age_h = int(
                        (datetime.now(timezone.utc) - m.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                    )
                    lines.append(f"  • {m.content[:80]} _(há {age_h}h)_")
                await send_proactive_message(
                    db,
                    user_id,
                    "\n".join(lines),
                    subject="stale_followup_nudge",
                    proactive_category="followup",
                    trigger_type="routine",
                )

    except Exception:
        logger.exception("Reminder check failed")
    finally:
        db.close()


async def _cleanup_browser_sessions() -> None:
    db = SessionLocal()
    try:
        from app.services import browser_service
        from app.utils.browser_utils import clean_old_browser_artifacts
        expired = await browser_service.expire_old_sessions(db)
        cleaned = clean_old_browser_artifacts(db, older_than_days=1)
        logger.info("Browser cleanup: expired=%d sessions, cleaned=%d artifacts", expired, cleaned)
    except Exception:
        logger.exception("Browser session cleanup failed")
    finally:
        db.close()


async def start_scheduler() -> None:
    global _scheduler_task
    if not settings.proactive_features_enabled:
        logger.info("Proactive features disabled, scheduler not started")
        return
    _scheduler_task = asyncio.create_task(_run_scheduler_loop())
    logger.info("Scheduler task created")


async def stop_scheduler() -> None:
    global _running, _scheduler_task
    _running = False
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    _scheduler_task = None
    logger.info("Scheduler stopped")
