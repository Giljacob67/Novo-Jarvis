import sys
import os

# Manually add current directory to path
sys.path.append(os.getcwd())

from app.utils.encryption import encrypt_data, decrypt_data
from app.config import settings

def test_encryption_decryption():
    # Setup a dummy key for testing
    settings.google_encryption_key = "dGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXlfdGVzdF9rZXk=" # 32 byte base64
    
    original_text = "secret_token_123"
    encrypted = encrypt_data(original_text)
    print(f"Original: {original_text}")
    print(f"Encrypted: {encrypted}")
    
    decrypted = decrypt_data(encrypted)
    print(f"Decrypted: {decrypted}")
    
    if decrypted == original_text:
        print("✅ Encryption/Decryption test passed!")
    else:
        print("❌ Encryption/Decryption test failed!")

if __name__ == "__main__":
    test_encryption_decryption()
