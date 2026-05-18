"""Token encryption utilities using Fernet symmetric encryption."""

from __future__ import annotations

import os
from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV = "AAP_TOKEN_ENCRYPTION_KEY"
_fernet: Fernet | None = None


def ensure_encryption_key_configured() -> None:
    """Ensure the API token encryption key is configured."""
    if not os.environ.get(_KEY_ENV, "").strip():
        raise RuntimeError(f"{_KEY_ENV} must be set for API token encryption")


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance, deriving the key from the environment."""
    global _fernet  # noqa: PLW0603
    if _fernet is not None:
        return _fernet

    ensure_encryption_key_configured()
    raw_key = os.environ[_KEY_ENV].strip()

    key_bytes = urlsafe_b64encode(sha256(raw_key.encode()).digest())
    _fernet = Fernet(key_bytes)
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string, returning the ciphertext as a string."""
    if not plaintext:
        return ""
    return str(_get_fernet().encrypt(plaintext.encode()).decode())


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a token string.

    Plaintext legacy tokens are returned unchanged for backward compatibility.
    Encrypted tokens must be decryptable with the configured key.
    """
    if not ciphertext:
        return ""
    if not ciphertext.startswith("gAAAAA"):
        return ciphertext
    try:
        return str(_get_fernet().decrypt(ciphertext.encode()).decode())
    except InvalidToken as exc:
        raise ValueError(
            "Stored token cannot be decrypted with the configured AAP_TOKEN_ENCRYPTION_KEY"
        ) from exc
