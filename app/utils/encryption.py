import base64
import logging
from cryptography.fernet import Fernet
from app.config import settings

logger = logging.getLogger(__name__)

def _get_fernet() -> Fernet | None:
    key = settings.google_encryption_key
    if not key:
        return None
    try:
        # Ensure key is base64 encoded and 32 bytes
        return Fernet(key.encode())
    except Exception:
        logger.exception("Invalid GOOGLE_ENCRYPTION_KEY format")
        return None

def encrypt_data(data: str) -> str:
    """Encrypts a string using Fernet (AES)."""
    if not data:
        return ""
    fernet = _get_fernet()
    if not fernet:
        # Fallback to plain text if no key is provided (warning logged elsewhere)
        return data
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    """Decrypts a Fernet-encrypted string."""
    if not encrypted_data:
        return ""
    fernet = _get_fernet()
    if not fernet:
        return encrypted_data
    try:
        return fernet.decrypt(encrypted_data.encode()).decode()
    except Exception:
        # If decryption fails, it might be plain text or a different key
        logger.warning("Decryption failed; returning original data")
        return encrypted_data
