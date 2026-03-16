import pytest
from app.utils.encryption import encrypt_data, decrypt_data
from app.config import settings

def test_encryption_decryption():
    # Setup a dummy key for testing
    settings.google_encryption_key = "dGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXk=" # 32 byte base64
    
    original_text = "secret_token_123"
    encrypted = encrypt_data(original_text)
    assert encrypted != original_text
    
    decrypted = decrypt_data(encrypted)
    assert decrypted == original_text

def test_no_key_fallback():
    settings.google_encryption_key = ""
    original_text = "plain_text"
    encrypted = encrypt_data(original_text)
    assert encrypted == original_text
    
    decrypted = decrypt_data(encrypted)
    assert decrypted == original_text

def test_invalid_data_recovery():
    settings.google_encryption_key = "dGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXk="
    invalid_encrypted = "not_encrypted_at_all"
    decrypted = decrypt_data(invalid_encrypted)
    assert decrypted == invalid_encrypted
