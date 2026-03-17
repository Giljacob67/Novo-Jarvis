import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services import google_oauth_service

logger = logging.getLogger(__name__)

DRIVE_SCOPES_MISSING = (
    "Sua conta Google está conectada, mas sem permissões de Drive. "
    "Use /connectgoogle para reconectar com escopo de Drive."
)


def _check_drive(db: Session, user_id: str) -> str | None:
    status = google_oauth_service.get_status(db, user_id)
    if not status.get("connected"):
        return "Google não conectado. Use /connectgoogle para conectar sua conta."
    if not status.get("drive_enabled"):
        return DRIVE_SCOPES_MISSING
    return None


def _get_drive_service(db: Session, user_id: str):
    from googleapiclient.discovery import build

    creds = google_oauth_service.get_credentials(db, user_id)
    if creds is None:
        return None
    return build("drive", "v3", credentials=creds)


async def list_files(db: Session, user_id: str, limit: int = 10) -> dict[str, Any]:
    error = _check_drive(db, user_id)
    if error:
        return {"error": error}

    service = _get_drive_service(db, user_id)
    if service is None:
        return {"error": "Não foi possível autenticar no Google Drive. Use /connectgoogle novamente."}

    try:
        result = service.files().list(
            pageSize=max(1, min(limit, 50)),
            q="trashed=false",
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName))",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        logger.exception("Drive list_files error for user=%s", user_id)
        return {"error": f"Erro ao listar arquivos do Drive: {e}"}

    files = result.get("files", [])
    return {"files": files, "count": len(files)}


async def search_files(db: Session, user_id: str, query: str, limit: int = 10) -> dict[str, Any]:
    error = _check_drive(db, user_id)
    if error:
        return {"error": error}

    service = _get_drive_service(db, user_id)
    if service is None:
        return {"error": "Não foi possível autenticar no Google Drive. Use /connectgoogle novamente."}

    safe_query = query.replace("'", "\\'")
    drive_q = f"trashed=false and name contains '{safe_query}'"
    try:
        result = service.files().list(
            pageSize=max(1, min(limit, 50)),
            q=drive_q,
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName))",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        logger.exception("Drive search_files error for user=%s query=%s", user_id, query)
        return {"error": f"Erro ao buscar arquivos no Drive: {e}"}

    files = result.get("files", [])
    return {"files": files, "count": len(files), "query": query}
