"""Token encryption utilities using Fernet symmetric encryption."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV = "AAP_TOKEN_ENCRYPTION_KEY"

_fernet: Fernet | None = None
_fernet_key: str | None = None


def ensure_encryption_key_configured() -> None:
    """Ensure the API token encryption key is configured."""
    if not os.environ.get(_KEY_ENV, "").strip():
        raise RuntimeError(f"{_KEY_ENV} must be set for API token encryption")


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance, deriving the key from the environment."""
    global _fernet, _fernet_key
    raw = os.environ.get(_KEY_ENV, "")
    if not raw.strip():
        raise RuntimeError(f"{_KEY_ENV} must be set for API token encryption")
    if _fernet is not None and _fernet_key == raw:
        return _fernet
    # Derive a 32-byte urlsafe key via SHA256 so any string works.
    digest = hashlib.sha256(raw.encode()).digest()
    key_bytes = base64.urlsafe_b64encode(digest)
    _fernet = Fernet(key_bytes)
    _fernet_key = raw
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string, returning the ciphertext as a string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a token string.

    Plaintext legacy tokens (not Fernet) are returned unchanged for
    backward compatibility.
    """
    if not ciphertext.startswith("gAAAAA"):
        return ciphertext
    # Need key configured to decrypt
    try:
        f = _get_fernet()
    except RuntimeError as err:
        raise ValueError(
            "Stored token cannot be decrypted with the configured AAP_TOKEN_ENCRYPTION_KEY"
        ) from err
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Stored token cannot be decrypted with the configured AAP_TOKEN_ENCRYPTION_KEY"
        ) from e
