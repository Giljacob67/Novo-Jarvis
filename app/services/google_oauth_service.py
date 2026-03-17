import json
import logging
import secrets
import base64
import hmac
import hashlib
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from app.config import settings
from app.models.google_credential import GoogleCredential
from app.utils.encryption import encrypt_data, decrypt_data

logger = logging.getLogger(__name__)

_pending_states: dict[str, str] = {}
_STATE_TTL_SECONDS = 15 * 60

GMAIL_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
}


def _get_scopes() -> list[str]:
    return settings.all_google_scopes.split()


def _state_signing_key() -> bytes:
    # Google client secret is always present when OAuth is enabled.
    secret = settings.google_client_secret or settings.telegram_webhook_secret
    return secret.encode("utf-8")


def _urlsafe_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _urlsafe_b64_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


def _create_signed_state(user_id: str) -> str:
    payload = {
        "u": user_id,
        "e": int(time.time()) + _STATE_TTL_SECONDS,
        "n": secrets.token_urlsafe(8),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_part = _urlsafe_b64(payload_bytes)
    signature = hmac.new(_state_signing_key(), payload_part.encode("utf-8"), hashlib.sha256).digest()
    sig_part = _urlsafe_b64(signature)
    return f"{payload_part}.{sig_part}"


def _verify_signed_state(state: str) -> str | None:
    try:
        payload_part, sig_part = state.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(_state_signing_key(), payload_part.encode("utf-8"), hashlib.sha256).digest()
    provided = _urlsafe_b64_decode(sig_part)
    if not hmac.compare_digest(expected, provided):
        return None

    try:
        payload = json.loads(_urlsafe_b64_decode(payload_part).decode("utf-8"))
    except Exception:
        return None

    user_id = str(payload.get("u", "")).strip()
    expires_at = int(payload.get("e", 0))
    if not user_id or expires_at < int(time.time()):
        return None
    return user_id


def _make_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.effective_google_redirect_uri],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=_get_scopes(),
        redirect_uri=settings.effective_google_redirect_uri,
    )
    return flow


def get_auth_url(user_id: str) -> str:
    flow = _make_flow()
    # Signed state makes callback validation work across restarts/instances.
    state = _create_signed_state(user_id)
    # Keep legacy map assignment for temporary compatibility (can be removed later).
    _pending_states[state] = user_id

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    return auth_url


def exchange_code(db: Session, code: str, state: str) -> GoogleCredential:
    user_id = _verify_signed_state(state)
    if user_id is None:
        # Legacy fallback: previous in-memory state format.
        user_id = _pending_states.pop(state, None)
    if user_id is None:
        raise ValueError("State inválido ou expirado. Tente conectar novamente via /connectgoogle.")

    flow = _make_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.exception("Google OAuth token exchange failed")
        msg = str(exc)
        if "invalid_grant" in msg.lower():
            raise ValueError(
                "Código OAuth inválido ou expirado. Use /connectgoogle novamente e conclua a autorização imediatamente."
            ) from exc
        if "redirect_uri_mismatch" in msg.lower():
            raise ValueError(
                "redirect_uri_mismatch: confira GOOGLE_REDIRECT_URI/APP_BASE_URL e a URI autorizada no Google Cloud."
            ) from exc
        raise ValueError(f"Falha ao trocar code por token no Google OAuth: {msg}") from exc

    creds = flow.credentials

    token_expiry = None
    if creds.expiry:
        token_expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry.tzinfo is None else creds.expiry

    existing = db.query(GoogleCredential).filter(GoogleCredential.user_id == user_id).first()
    if not creds.refresh_token and (existing is None or not existing.refresh_token):
        raise ValueError(
            "O Google não retornou um refresh_token. "
            "Remova o acesso do app em https://myaccount.google.com/permissions "
            "e tente novamente via /connectgoogle."
        )
    if existing:
        existing.access_token = encrypt_data(creds.token)
        existing.refresh_token = encrypt_data(creds.refresh_token) if creds.refresh_token else existing.refresh_token
        existing.token_expiry = token_expiry
        existing.scope = " ".join(creds.scopes or _get_scopes())
        existing.token_type = "Bearer"
        existing.raw_json = encrypt_data(creds.to_json())
        existing.updated_at = datetime.now(timezone.utc)
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("Failed to persist updated Google credential")
            raise ValueError(f"Falha ao salvar credenciais Google no banco: {exc}") from exc
        db.refresh(existing)
        return existing

    credential = GoogleCredential(
        user_id=user_id,
        access_token=encrypt_data(creds.token),
        refresh_token=encrypt_data(creds.refresh_token) if creds.refresh_token else None,
        token_expiry=token_expiry,
        scope=" ".join(creds.scopes or _get_scopes()),
        token_type="Bearer",
        raw_json=encrypt_data(creds.to_json()),
    )
    db.add(credential)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Failed to persist new Google credential")
        raise ValueError(f"Falha ao salvar credenciais Google no banco: {exc}") from exc
    db.refresh(credential)
    return credential


def refresh_credentials(db: Session, user_id: str) -> Credentials | None:
    cred = db.query(GoogleCredential).filter(GoogleCredential.user_id == user_id).first()
    if cred is None or not cred.access_token:
        return None

    expiry = cred.token_expiry
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    credentials = Credentials(
        token=decrypt_data(cred.access_token),
        refresh_token=decrypt_data(cred.refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=cred.scope.split() if cred.scope else _get_scopes(),
        expiry=expiry,
    )

    if credentials.expired and credentials.refresh_token:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        try:
            credentials.refresh(GoogleAuthRequest())
            new_expiry = credentials.expiry
            if new_expiry and new_expiry.tzinfo is None:
                new_expiry = new_expiry.replace(tzinfo=timezone.utc)
            cred.access_token = encrypt_data(credentials.token)
            cred.token_expiry = new_expiry
            cred.updated_at = datetime.now(timezone.utc)
            db.commit()
            logger.info("Refreshed Google token for user=%s", user_id)
        except Exception:
            logger.exception("Failed to refresh Google token for user=%s", user_id)
            return None

    return credentials


def get_credentials(db: Session, user_id: str) -> Credentials | None:
    return refresh_credentials(db, user_id)


def has_gmail_scopes(db: Session, user_id: str) -> bool:
    cred = db.query(GoogleCredential).filter(GoogleCredential.user_id == user_id).first()
    if cred is None or not cred.scope:
        return False
    granted = set(cred.scope.split())
    return GMAIL_SCOPES.issubset(granted)


def get_status(db: Session, user_id: str) -> dict[str, Any]:
    cred = db.query(GoogleCredential).filter(GoogleCredential.user_id == user_id).first()
    if cred is None or not cred.access_token:
        return {
            "connected": False,
            "scope": "",
            "token_expiry": None,
            "gmail_enabled": False,
            "calendar_enabled": False,
            "tasks_enabled": False,
        }

    granted = set(cred.scope.split()) if cred.scope else set()
    return {
        "connected": True,
        "scope": cred.scope,
        "token_expiry": cred.token_expiry.isoformat() if cred.token_expiry else None,
        "has_refresh_token": cred.refresh_token is not None,
        "gmail_enabled": GMAIL_SCOPES.issubset(granted),
        "calendar_enabled": "https://www.googleapis.com/auth/calendar.events" in granted,
        "tasks_enabled": "https://www.googleapis.com/auth/tasks" in granted,
    }


async def revoke_and_disconnect(db: Session, user_id: str) -> dict[str, Any]:
    cred = db.query(GoogleCredential).filter(GoogleCredential.user_id == user_id).first()
    if cred is None:
        return {"disconnected": False, "message": "Nenhuma conta Google conectada."}

    revoked = False
    encrypted_token = cred.refresh_token or cred.access_token
    token_to_revoke = decrypt_data(encrypted_token) if encrypted_token else ""
    if token_to_revoke:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": token_to_revoke},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                revoked = resp.status_code == 200
                if not revoked:
                    logger.warning("Google token revocation returned %s for user=%s", resp.status_code, user_id)
        except Exception:
            logger.exception("Failed to revoke Google token for user=%s (best-effort)", user_id)

    db.delete(cred)
    db.commit()
    logger.info("Disconnected Google for user=%s (revoked=%s)", user_id, revoked)

    return {"disconnected": True, "revoked": revoked}
