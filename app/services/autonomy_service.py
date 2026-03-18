from sqlalchemy.orm import Session

from app.config import settings
from app.models.memory_item import MemoryItem


AUTONOMY_MODES = ("conservative", "hybrid_safe", "aggressive")
AUTONOMY_PREF_PREFIX = "autonomy_mode:"

MUTATING_TOOLS = {
    "create_task",
    "update_task",
    "delete_task",
    "create_event",
    "update_event",
    "delete_event",
    "send_email_draft",
    "create_email_draft",
    "create_reply_draft",
    "browser_click",
    "browser_fill",
    "browser_select_option",
    "browser_download_file",
}

HIGH_RISK_TOOLS = {
    "delete_task",
    "delete_event",
    "update_task",
    "update_event",
    "send_email_draft",
    "browser_click",
    "browser_fill",
    "browser_select_option",
    "browser_download_file",
}

CRITICAL_TOOLS = {
    "delete_task",
    "delete_event",
    "send_email_draft",
}


def get_user_autonomy_mode(db: Session, user_id: str) -> str:
    item = (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.category == "preference",
            MemoryItem.is_active == True,
            MemoryItem.content.like(f"{AUTONOMY_PREF_PREFIX}%"),
        )
        .order_by(MemoryItem.created_at.desc())
        .first()
    )
    if item and item.content:
        mode = item.content.replace(AUTONOMY_PREF_PREFIX, "", 1).strip()
        if mode in AUTONOMY_MODES:
            return mode
    return settings.autonomy_default_mode


def set_user_autonomy_mode(db: Session, user_id: str, mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized not in AUTONOMY_MODES:
        normalized = settings.autonomy_default_mode

    existing = (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.category == "preference",
            MemoryItem.content.like(f"{AUTONOMY_PREF_PREFIX}%"),
        )
        .all()
    )
    for item in existing:
        db.delete(item)
    db.flush()

    db.add(
        MemoryItem(
            user_id=user_id,
            category="preference",
            content=f"{AUTONOMY_PREF_PREFIX}{normalized}",
            source="command",
            is_active=True,
        )
    )
    db.commit()
    return normalized


def should_require_confirmation(mode: str, tool_name: str) -> bool:
    if mode == "conservative":
        return tool_name in MUTATING_TOOLS
    if mode == "aggressive":
        return tool_name in CRITICAL_TOOLS
    return tool_name in HIGH_RISK_TOOLS


def evaluate_tool_execution(db: Session, user_id: str, tool_name: str) -> dict:
    mode = get_user_autonomy_mode(db, user_id)
    requires = should_require_confirmation(mode, tool_name)
    if not requires:
        return {"allow": True, "mode": mode}
    return {
        "allow": False,
        "mode": mode,
        "message": (
            f"Ação sensível bloqueada pelo modo de autonomia atual ({mode}). "
            "Se quiser, ajuste com /autonomy."
        ),
    }


def autonomy_matrix(mode: str) -> dict[str, str]:
    if mode == "conservative":
        return {
            "baixo_risco": "pede confirmação",
            "sensivel": "sempre pede confirmação",
            "critico": "sempre pede confirmação",
        }
    if mode == "aggressive":
        return {
            "baixo_risco": "executa automaticamente",
            "sensivel": "executa automaticamente",
            "critico": "pede confirmação",
        }
    return {
        "baixo_risco": "executa automaticamente",
        "sensivel": "pede confirmação",
        "critico": "pede confirmação",
    }
