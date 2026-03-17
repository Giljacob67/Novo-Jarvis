import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.google_oauth_service import get_credentials, get_status

logger = logging.getLogger(__name__)


def _format_google_tasks_error(exc: Exception, action: str) -> str:
    status = getattr(getattr(exc, "resp", None), "status", None)
    detail = ""
    content = getattr(exc, "content", None)
    if isinstance(content, (bytes, bytearray)):
        detail = content.decode("utf-8", errors="ignore")
    elif content is not None:
        detail = str(content)
    else:
        detail = str(exc)

    if status == 401:
        return "Sessão Google expirada ou inválida. Use /connectgoogle para reconectar."
    if status == 403:
        if "insufficient" in detail.lower() or "scope" in detail.lower():
            return "Permissão insuficiente para Google Tasks. Use /connectgoogle para autorizar Tasks."
        return "Acesso negado pelo Google Tasks (403). Verifique permissões da conta."
    if status == 404:
        return "Lista de tarefas padrão não encontrada. Abra o Google Tasks uma vez e tente novamente."
    return f"Erro no Google Tasks ao {action}. Tente novamente em instantes."


def _build_service(db: Session, user_id: str):
    status = get_status(db, user_id)
    if not status.get("connected") or not status.get("tasks_enabled"):
        return None
    creds = get_credentials(db, user_id)
    if creds is None:
        return None
    from googleapiclient.discovery import build
    return build("tasks", "v1", credentials=creds)


async def list_task_lists(db: Session, user_id: str) -> list[dict[str, Any]]:
    service = _build_service(db, user_id)
    if service is None:
        return []

    try:
        result = service.tasklists().list(maxResults=20).execute()
        return [
            {"id": tl.get("id", ""), "title": tl.get("title", "")}
            for tl in result.get("items", [])
        ]
    except Exception:
        logger.exception("Failed to list task lists for user=%s", user_id)
        return []


async def list_tasks(
    db: Session,
    user_id: str,
    tasklist_id: str | None = None,
    show_completed: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    service = _build_service(db, user_id)
    if service is None:
        return []

    tl_id = tasklist_id or "@default"
    try:
        result = service.tasks().list(
            tasklist=tl_id,
            maxResults=limit,
            showCompleted=show_completed,
            showHidden=False,
        ).execute()
        items = result.get("items", [])
        return [
            {
                "id": t.get("id", ""),
                "title": t.get("title", ""),
                "notes": t.get("notes", ""),
                "due": t.get("due", ""),
                "status": t.get("status", "needsAction"),
            }
            for t in items
            if t.get("title", "").strip()
        ]
    except Exception:
        logger.exception("Failed to list tasks for user=%s", user_id)
        return []


async def create_task(
    db: Session,
    user_id: str,
    title: str,
    notes: str | None = None,
    due: str | None = None,
    tasklist_id: str | None = None,
) -> dict[str, Any]:
    if not title.strip():
        return {"error": "Título da tarefa vazio. Informe um título para criar a tarefa."}

    service = _build_service(db, user_id)
    if service is None:
        status = get_status(db, user_id)
        if not status.get("connected"):
            return {"error": "Google não conectado. Use /connectgoogle para conectar."}
        if not status.get("tasks_enabled"):
            return {"error": "Google Tasks não autorizado. Use /connectgoogle para liberar o escopo de Tasks."}
        return {"error": "Não foi possível validar credenciais Google. Use /connectgoogle para reconectar."}

    tl_id = tasklist_id or "@default"
    body: dict[str, Any] = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        if "T" not in due:
            due = f"{due}T00:00:00.000Z"
        body["due"] = due

    try:
        created = service.tasks().insert(tasklist=tl_id, body=body).execute()
        logger.info("Created task id=%s for user=%s", created.get("id"), user_id)
        return {
            "id": created.get("id"),
            "title": created.get("title"),
            "status": created.get("status"),
        }
    except Exception as e:
        logger.exception("Failed to create task for user=%s", user_id)
        return {"error": _format_google_tasks_error(e, "criar tarefa")}


async def complete_task(
    db: Session,
    user_id: str,
    task_id: str,
    tasklist_id: str | None = None,
) -> dict[str, Any]:
    service = _build_service(db, user_id)
    if service is None:
        return {"error": "Google não conectado. Use /connectgoogle para conectar."}

    tl_id = tasklist_id or "@default"
    try:
        task = service.tasks().get(tasklist=tl_id, task=task_id).execute()
        task["status"] = "completed"
        updated = service.tasks().update(tasklist=tl_id, task=task_id, body=task).execute()
        logger.info("Completed task id=%s for user=%s", task_id, user_id)
        return {"id": updated.get("id"), "title": updated.get("title"), "status": "completed"}
    except Exception:
        logger.exception("Failed to complete task for user=%s", user_id)
        return {"error": "Erro ao completar tarefa no Google Tasks."}


async def update_task(
    db: Session,
    user_id: str,
    task_id: str,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    tasklist_id: str | None = None,
) -> dict[str, Any]:
    service = _build_service(db, user_id)
    if service is None:
        return {"error": "Google não conectado."}

    tl_id = tasklist_id or "@default"
    try:
        task = service.tasks().get(tasklist=tl_id, task=task_id).execute()
        if title:
            task["title"] = title
        if notes is not None:
            task["notes"] = notes
        if due:
            if "T" not in due:
                due = f"{due}T00:00:00.000Z"
            task["due"] = due

        updated = service.tasks().update(tasklist=tl_id, task=task_id, body=task).execute()
        return {"id": updated.get("id"), "title": updated.get("title"), "status": "updated"}
    except Exception:
        logger.exception("Failed to update task id=%s for user=%s", task_id, user_id)
        return {"error": "Erro ao atualizar tarefa no Google Tasks."}


async def delete_task(
    db: Session,
    user_id: str,
    task_id: str,
    tasklist_id: str | None = None,
) -> dict[str, Any]:
    service = _build_service(db, user_id)
    if service is None:
        return {"error": "Google não conectado."}

    tl_id = tasklist_id or "@default"
    try:
        service.tasks().delete(tasklist=tl_id, task=task_id).execute()
        return {"id": task_id, "status": "deleted"}
    except Exception:
        logger.exception("Failed to delete task id=%s for user=%s", task_id, user_id)
        return {"error": "Erro ao excluir tarefa no Google Tasks."}
