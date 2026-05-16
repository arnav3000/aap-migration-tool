"""Token encryption utilities using Fernet symmetric encryption."""

from __future__ import annotations

import os
from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet

_KEY_ENV = "AAP_TOKEN_ENCRYPTION_KEY"
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance, deriving the key from the environment."""
    global _fernet  # noqa: PLW0603
    if _fernet is not None:
        return _fernet

    raw_key = os.environ.get(_KEY_ENV, "")
    if not raw_key:
        raw_key = "aap-migration-default-key-change-me"

    key_bytes = urlsafe_b64encode(sha256(raw_key.encode()).digest())
    _fernet = Fernet(key_bytes)
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string, returning the ciphertext as a string."""
    if not plaintext:
        return ""
    return str(_get_fernet().encrypt(plaintext.encode()).decode())


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a token string. Returns empty string if input is empty or decryption fails."""
    if not ciphertext:
        return ""
    try:
        return str(_get_fernet().decrypt(ciphertext.encode()).decode())
    except Exception:
        return ciphertext
