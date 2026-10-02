"""Token encryption for stored AAP connections.

Uses Fernet (``cryptography`` package, already a project dependency).
The key is resolved in order:

1. ``AAP_BRIDGE_API_KEY`` environment variable (Fernet key, urlsafe-base64).
2. ``<api-db-dir>/api_fernet.key`` file (created on first use, mode ``0o600``).

API request authentication (``AAP_BRIDGE_API_TOKEN``) is separate from the
Fernet storage key: the former gates HTTP access, the latter encrypts tokens
at rest.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from aap_migration.api.models import api_db_path

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _key_file() -> str:
    # Called only when AAP_BRIDGE_API_KEY is unset (get_fernet checks the
    # env first). Non-file DB URLs (e.g. postgres://) have no stable
    # sibling directory: fail closed instead of minting CWD-dependent key
    # files that diverge across replicas and flap decrypts behind the load
    # balancer. File-backed sqlite (the supported HA story) resolves next
    # to the DB file; replicas must share AAP_BRIDGE_API_KEY.
    db_path = api_db_path()
    if "://" in db_path and not db_path.startswith("sqlite"):
        raise ValueError(
            "AAP_BRIDGE_API_DB is a non-file URL with no AAP_BRIDGE_API_KEY "
            "configured: set AAP_BRIDGE_API_KEY (shared across replicas) "
            "before storing connections."
        )
    if "://" in db_path:
        # sqlite:///... URL form: strip the scheme to find the sibling dir.
        fs_path = db_path
        for scheme in ("sqlite:///", "sqlite://"):
            if fs_path.startswith(scheme):
                fs_path = fs_path[len(scheme) :]
                break
        directory = os.path.dirname(os.path.abspath(fs_path)) or "."
        return os.path.join(directory, "api_fernet.key")
    directory = os.path.dirname(os.path.abspath(db_path)) or "."
    return os.path.join(directory, "api_fernet.key")


def get_fernet() -> Fernet:
    """Return a Fernet instance for the configured key (generating if needed).

    The key file is created atomically (O_EXCL) so concurrent first-use
    cannot truncate each other's keys. Rotation is explicit: changing
    AAP_BRIDGE_API_KEY (or the key file) without re-encrypting stored
    tokens makes them undecryptable by design; decrypt_token() reports an
    operator-actionable error instead of failing jobs silently.
    """
    env_key = os.environ.get("AAP_BRIDGE_API_KEY")
    if env_key:
        return Fernet(env_key.encode() if isinstance(env_key, str) else env_key)

    key_path = _key_file()
    if os.path.exists(key_path):
        with open(key_path, "rb") as fh:
            return Fernet(fh.read().strip())

    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(key_path, "rb") as fh:
            return Fernet(fh.read().strip())
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except BaseException:
        try:
            os.unlink(key_path)
        except OSError:
            pass
        raise
    return Fernet(key)


def fernet_key_fingerprint() -> str:
    """SHA-256 fingerprint of the active Fernet key material (hash, not key).

    Used to pin the encryption key at job submit time: a rotation
    between submit (202) and execution must fail fast with a
    drain-before-rotate error instead of failing N queued jobs at
    decrypt time with per-row errors. Never logs or returns key bytes.
    """
    env_key = os.environ.get("AAP_BRIDGE_API_KEY")
    if env_key:
        raw = env_key.encode()
    else:
        key_path = _key_file()
        try:
            with open(key_path, "rb") as fh:
                raw = fh.read().strip()
        except OSError:
            # Key file does not exist yet (first submit will generate
            # it via get_fernet): fingerprint the resolved instance.
            # get_fernet has no accessor for raw bytes, so re-read the
            # file it just created.
            get_fernet()
            with open(key_path, "rb") as fh:
                raw = fh.read().strip()
    return hashlib.sha256(raw).hexdigest()


def encrypt_token(plain: str) -> str:
    """Encrypt a plaintext token for storage."""
    token: bytes = get_fernet().encrypt(plain.encode())
    return token.decode()


def decrypt_token(cipher: str) -> str:
    """Decrypt a stored token. Raises ValueError if undecryptable."""
    try:
        plain: bytes = get_fernet().decrypt(cipher.encode())
        return plain.decode()
    except InvalidToken as exc:
        raise ValueError(
            "Stored token cannot be decrypted with the current API key. "
            "Set AAP_BRIDGE_API_KEY to the original key or re-create the connection."
        ) from exc


# -- API request authentication -------------------------------------------
log = logging.getLogger("aap_migration.api.security")

# In-memory per-IP 401 throttling state:
# {bucket: [count, window_start]} where bucket is the client IP.
# Buckets are keyed by IP alone: keying by presented-key identity lets an
# attacker rotate X-API-Key per guess and never trip the 20/min 429.
# Trade-off: one abusive IP throttles legitimate users sharing the same
# egress IP until the 60s window lapses (a success resets the IP bucket).
# Deployments behind a shared NAT/ingress should still front this with an
# external rate limiter for cross-IP abuse.
_auth_failures: dict[str, list[float]] = {}
_auth_lock = threading.Lock()
_AUTH_WINDOW_S = 60.0
_AUTH_MAX_FAILURES = 20
# Bound the bucket so weeks of exposure (or one IPv6-range scan) cannot
# grow the hot-path dict without bound: inserts past the cap purge expired
# entries first, then evict the oldest windows.
_AUTH_MAX_TRACKED_IPS = 5000


def _sweep_auth_failures(now: float) -> None:
    """Drop expired windows. Callers hold ``_auth_lock``."""
    expired = [b for b, entry in _auth_failures.items() if (now - entry[1]) > _AUTH_WINDOW_S]
    for b in expired:
        _auth_failures.pop(b, None)


# (Fail-closed: no anonymous-access warning state is kept; every
# unauthenticated request without a configured token is a 401.)


def _expected_tokens() -> list[str]:
    tokens: list[str] = []
    primary = os.environ.get("AAP_BRIDGE_API_TOKEN", "")
    if primary:
        tokens.append(primary)
        if len(primary) < 16:
            log.warning("AAP_BRIDGE_API_TOKEN is shorter than 16 chars; use a longer random token.")
    secondary = os.environ.get("AAP_BRIDGE_API_TOKEN_SECONDARY", "")
    if secondary:
        tokens.append(secondary)
        if len(secondary) < 16:
            log.warning(
                "AAP_BRIDGE_API_TOKEN_SECONDARY is shorter than 16 chars; "
                "use a longer random token."
            )
    return tokens


def _matches_any(candidate: str, expected_tokens: list[str]) -> bool:
    candidate_hash = hashlib.sha256(candidate.encode()).hexdigest()
    for expected in expected_tokens:
        if hmac.compare_digest(
            candidate_hash,
            hashlib.sha256(expected.encode()).hexdigest(),
        ):
            return True
    return False


def _client_ip(request: Request | None) -> str:
    try:
        if request is not None and request.client is not None:
            return request.client.host or "unknown"
    except Exception:
        pass
    return "unknown"


def _throttle_bucket(ip: str) -> str:
    """Bucket for 401 throttling: client IP only.

    The presented-key identity is deliberately not part of the bucket:
    per-key buckets let an attacker rotate X-API-Key per guess and never
    trip the 20/min 429.
    """
    return ip or "unknown"


def _record_auth_failure(bucket: str) -> None:
    now = time.monotonic()
    with _auth_lock:
        entry = _auth_failures.get(bucket)
        if entry is None or (now - entry[1]) > _AUTH_WINDOW_S:
            entry = [0.0, now]
            _auth_failures[bucket] = entry
        entry[0] += 1
        if len(_auth_failures) > _AUTH_MAX_TRACKED_IPS:
            _sweep_auth_failures(now)
            while len(_auth_failures) > _AUTH_MAX_TRACKED_IPS:
                # Evict the oldest window first (least likely to reoffend).
                oldest = min(_auth_failures.items(), key=lambda kv: kv[1][1])[0]
                _auth_failures.pop(oldest, None)


def _auth_failure_count(bucket: str) -> int:
    now = time.monotonic()
    with _auth_lock:
        entry = _auth_failures.get(bucket)
        if entry is None:
            return 0
        if (now - entry[1]) > _AUTH_WINDOW_S:
            _auth_failures.pop(bucket, None)
            return 0
        return int(entry[0])


def _reset_auth_failures(bucket: str) -> None:
    with _auth_lock:
        _auth_failures.pop(bucket, None)


def require_api_key(
    api_key: str | None = Depends(_api_key_header),
    request: Request = None,
) -> None:
    """Gate every API route behind a bearer-style API key.

    The expected key comes from ``AAP_BRIDGE_API_TOKEN`` with optional
    rotation secondary ``AAP_BRIDGE_API_TOKEN_SECONDARY`` (either accepted).
    Fail closed when no token is configured: requests are rejected with 401
    unless the explicit opt-in ``AAP_BRIDGE_ALLOW_ANON=1`` is set. The bind
    host is never consulted -- an earlier revision trusted the
    ``AAP_BRIDGE_API_HOST`` env default and fail-opened when the process was
    launched with an explicit non-loopback bind (e.g. ``uvicorn --host
    0.0.0.0``) that did not flow through that variable.     Uses
    :func:`hmac.compare_digest` (constant time) to avoid timing oracles.
    Per-IP 401 throttling: more than 20 failures in 60s yields
    429. A success resets its IP bucket; deployments behind a shared
    egress/NAT need an external
    rate limiter for cross-IP abuse.
    """
    expected_tokens = _expected_tokens()
    if not expected_tokens:
        if os.environ.get("AAP_BRIDGE_ALLOW_ANON", "") == "1":
            return None
        raise HTTPException(status_code=401, detail="API token required (set AAP_BRIDGE_API_TOKEN)")
    ip = _client_ip(request)
    bucket = _throttle_bucket(ip)
    if api_key is not None and _matches_any(api_key, expected_tokens):
        _reset_auth_failures(bucket)
        return None
    _record_auth_failure(bucket)
    if _auth_failure_count(bucket) > _AUTH_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed auth attempts")
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


# -- SSRF / URL validators (leaf home: utils.ssrf) ---------------------------
# Transport (base_client) and API layers share these via the leaf module so
# the dependency points the right way. Re-exported here for backward
# compatibility of ``from aap_migration.api.security import ...``.


def confine_path(path: str | Path, base: str | Path, *, label: str = "path") -> Path:
    """Resolve *path* and require it to stay under *base*.

    Raises ValueError when the resolved path escapes (``..`` traversal,
    symlink escape, or absolute path outside the base). Returns the resolved
    absolute Path otherwise.
    """
    base_resolved = Path(base).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base_resolved / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under {base_resolved}") from exc
    return resolved


def redact_backend_error(exc: BaseException) -> str:
    """Return a generic connectivity-failure message (no backend detail)."""
    _ = exc
    return "Connectivity test failed (see server logs for detail)"
