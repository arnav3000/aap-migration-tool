"""Token encryption for connection credentials stored at rest.

Uses Fernet symmetric encryption from the cryptography package.
The encryption key is read from AAP_BRIDGE_SECRET_KEY environment variable.
If not set, a key is generated and written to .secret_key for persistence.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.environ.get("AAP_BRIDGE_SECRET_KEY")

    if not key:
        key_file = Path(".secret_key")
        if key_file.exists():
            key = key_file.read_text().strip()
        if not key:
            key = Fernet.generate_key().decode()
            key_file.write_text(key)
            key_file.chmod(0o600)

    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token for storage."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored token."""
    if not ciphertext:
        return ciphertext
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ciphertext
