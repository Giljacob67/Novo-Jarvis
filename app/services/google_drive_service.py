import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services import google_oauth_service

logger = logging.getLogger(__name__)

DRIVE_SCOPES_MISSING = (
    "Sua conta Google está conectada, mas sem permissões de Drive. "
    "Use /connectgoogle para reconectar com escopo de Drive."
)
_MAX_SUMMARY_SOURCE_CHARS = 20000


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


def _effective_provider() -> str:
    from app.config import settings

    provider = (settings.llm_provider or "").strip().lower()
    return provider if provider in {"openai", "openrouter"} else "openai"


def _effective_model() -> str:
    from app.config import settings

    provider = _effective_provider()
    if provider == "openrouter" and settings.openrouter_model.strip():
        return settings.openrouter_model.strip()
    return settings.openai_model


def _get_llm_client() -> tuple[Any | None, str]:
    from app.config import settings
    from openai import OpenAI

    provider = _effective_provider()
    if provider == "openrouter":
        api_key = settings.openrouter_api_key or settings.openai_api_key
    else:
        api_key = settings.openai_api_key

    if not api_key:
        return None, provider

    if provider == "openrouter":
        headers: dict[str, str] = {"X-Title": "Jarvis Pessoal"}
        if settings.effective_base_url:
            headers["HTTP-Referer"] = settings.effective_base_url
        return OpenAI(api_key=api_key, base_url=settings.openrouter_base_url, default_headers=headers), provider

    return OpenAI(api_key=api_key), provider


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def _extract_text_content(service: Any, file_id: str, mime_type: str) -> tuple[str | None, str | None]:
    google_docs = "application/vnd.google-apps.document"
    google_sheets = "application/vnd.google-apps.spreadsheet"
    text_like = (
        mime_type.startswith("text/")
        or mime_type in {"application/json", "application/xml"}
    )

    try:
        if mime_type == google_docs:
            data = service.files().export_media(fileId=file_id, mimeType="text/plain").execute()
            return _decode_text(data if isinstance(data, (bytes, bytearray)) else bytes(data)), None

        if mime_type == google_sheets:
            data = service.files().export_media(fileId=file_id, mimeType="text/csv").execute()
            return _decode_text(data if isinstance(data, (bytes, bytearray)) else bytes(data)), None

        if text_like:
            data = service.files().get_media(fileId=file_id).execute()
            return _decode_text(data if isinstance(data, (bytes, bytearray)) else bytes(data)), None

        return None, (
            "Esse tipo de arquivo ainda não tem extração automática de conteúdo no Jarvis "
            f"(mime: {mime_type})."
        )
    except Exception as e:
        logger.exception("Drive extract_text_content error for file_id=%s", file_id)
        return None, f"Falha ao extrair conteúdo do arquivo: {e}"


def _extract_response_text(response: Any) -> str:
    final_text = ""
    output = getattr(response, "output", []) or []
    for item in output:
        if getattr(item, "type", "") != "message":
            continue
        for content_part in getattr(item, "content", []) or []:
            if getattr(content_part, "type", "") == "output_text":
                final_text += getattr(content_part, "text", "")
    return final_text.strip()


def _summarize_with_llm(title: str, text: str, focus: str | None = None) -> str:
    client, provider = _get_llm_client()
    if client is None:
        return (
            "Não consegui gerar resumo porque a LLM não está configurada. "
            "Defina OPENAI_API_KEY ou OPENROUTER_API_KEY."
        )

    focus_line = f"Foco solicitado pelo usuário: {focus}\n" if focus else ""
    system_prompt = (
        "Você é um assistente que resume documentos em pt-BR. "
        "Seja objetivo, útil e fiel ao texto."
    )
    user_prompt = (
        f"Documento: {title}\n"
        f"{focus_line}"
        "Tarefa: gere um resumo executivo em até 8 bullets e finalize com 3 ações práticas.\n\n"
        "Conteúdo:\n"
        f"{text[:_MAX_SUMMARY_SOURCE_CHARS]}"
    )

    try:
        if provider == "openrouter":
            resp = client.chat.completions.create(
                model=_effective_model(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            message = resp.choices[0].message if resp.choices else None
            content = getattr(message, "content", "") if message else ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            return str(content).strip() or "Não consegui gerar o resumo."

        resp = client.responses.create(
            model=_effective_model(),
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return _extract_response_text(resp) or "Não consegui gerar o resumo."
    except Exception as e:
        logger.exception("Drive summarize LLM error")
        return f"Falha ao gerar resumo: {e}"


async def get_file_details(db: Session, user_id: str, file_id: str) -> dict[str, Any]:
    error = _check_drive(db, user_id)
    if error:
        return {"error": error}

    service = _get_drive_service(db, user_id)
    if service is None:
        return {"error": "Não foi possível autenticar no Google Drive. Use /connectgoogle novamente."}

    try:
        item = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,modifiedTime,size,webViewLink,owners(displayName,emailAddress),driveId",
            supportsAllDrives=True,
        ).execute()
        return {"file": item}
    except Exception as e:
        logger.exception("Drive get_file_details error for user=%s file_id=%s", user_id, file_id)
        return {"error": f"Erro ao abrir arquivo no Drive: {e}"}


async def summarize_file(db: Session, user_id: str, file_id: str, focus: str | None = None) -> dict[str, Any]:
    details = await get_file_details(db, user_id, file_id)
    if "error" in details:
        return details

    file_item = details.get("file", {})
    mime_type = file_item.get("mimeType", "")
    file_name = file_item.get("name", file_id)

    service = _get_drive_service(db, user_id)
    if service is None:
        return {"error": "Não foi possível autenticar no Google Drive. Use /connectgoogle novamente."}

    text, extraction_error = _extract_text_content(service, file_id, mime_type)
    if extraction_error:
        return {"error": extraction_error, "file": file_item}
    if not text or not text.strip():
        return {"error": "Não consegui extrair texto útil desse arquivo.", "file": file_item}

    summary = _summarize_with_llm(file_name, text, focus=focus)
    return {
        "file": file_item,
        "summary": summary,
        "chars_used": min(len(text), _MAX_SUMMARY_SOURCE_CHARS),
    }


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
