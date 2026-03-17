import logging

from fastapi import APIRouter, Depends, Query, Header
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.services import google_oauth_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/google/start")
async def google_auth_start():
    if not settings.google_client_id or not settings.google_client_secret:
        return JSONResponse(
            status_code=501,
            content={"status": "not_implemented", "message": "Google OAuth não configurado. Defina GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET."},
        )

    user_id = settings.telegram_allowed_user_id or "default"
    auth_url = google_oauth_service.get_auth_url(user_id)
    return RedirectResponse(url=auth_url)


@router.get("/google/callback")
async def google_auth_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    db: Session = Depends(get_db),
):
    if error:
        logger.warning("Google OAuth error: %s", error)
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": f"Erro no OAuth do Google: {error}"},
        )

    if not code:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "Parâmetro 'code' ausente."},
        )

    if not state:
        return JSONResponse(
            status_code=403,
            content={"ok": False, "message": "Parâmetro 'state' ausente. Possível tentativa de CSRF."},
        )

    try:
        credential = google_oauth_service.exchange_code(db, code, state)
    except ValueError as e:
        error_msg = str(e)
        if "refresh_token" in error_msg.lower():
            return JSONResponse(status_code=400, content={"ok": False, "message": error_msg})
        return JSONResponse(status_code=403, content={"ok": False, "message": error_msg})

    logger.info("Google OAuth completed for user=%s", credential.user_id)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "message": "Conta Google conectada com sucesso! Volte ao Telegram e use /myday para ver seus dados reais.",
            "user_id": credential.user_id,
            "scope": credential.scope,
        },
    )


@router.get("/google/status")
async def google_auth_status(
    db: Session = Depends(get_db),
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
):
    from app.routes.telegram import _check_admin_key
    err = _check_admin_key(x_admin_key)
    if err:
        return err
        
    user_id = settings.telegram_allowed_user_id or "default"
    status = google_oauth_service.get_status(db, user_id)
    return status


@router.post("/google/disconnect")
async def google_disconnect(db: Session = Depends(get_db)):
    user_id = settings.telegram_allowed_user_id or "default"
    result = await google_oauth_service.revoke_and_disconnect(db, user_id)
    return result
