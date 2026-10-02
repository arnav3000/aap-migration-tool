"""CRUD operations for API-managed AAP connections."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from aap_migration.api.models import ApiActiveConfig, ApiConnection, api_session, init_api_db
from aap_migration.api.security import decrypt_token, encrypt_token

KINDS = ("source", "target")

# Connection-scope discriminator shared by every ``need`` parameter
# (store, context, services): one alias, one default, so a typo fails at
# the boundary instead of falling through to inference.
NeedScope = Literal["both", "source", "none"]

# Submit-time pair-fingerprint snapshot keys (single home for the
# ``_snapshot_*`` write in context.submit_pair_snapshot and every read in
# context.py, manager.py, _common.py and store.py). Import these instead of
# string literals so a typo fails at import/attribute time, not as a silent
# fingerprint miss that disables the credential-drift guard.
SNAPSHOT_SOURCE_ID = "_snapshot_source_id"
SNAPSHOT_TARGET_ID = "_snapshot_target_id"
SNAPSHOT_FP = "_snapshot_fp"
SNAPSHOT_NEED = "_snapshot_need"
SNAPSHOT_FERNET_FP = "_snapshot_fernet_fp"


def needs_target(need: NeedScope) -> bool:
    """True when the connection scope includes the target side.

    Single home for the three-valued ``need`` discriminator (``"both"`` /
    ``"source"`` / ``"none"``): ``need == "both"`` is the only scope that
    consumes the target connection.
    """
    return need == "both"


def needs_connections(need: NeedScope) -> bool:
    """True when the scope bears connections at all (anything but ``"none"``)."""
    return need != "none"


@contextmanager
def api_session_scope(db_path: str | None = None) -> Iterator[Session]:
    """Single home for store session handling: init DB then yield a session."""
    init_api_db(db_path)
    with api_session(db_path) as session:
        yield session


def _to_dict(conn: ApiConnection, *, include_token: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": conn.id,
        "name": conn.name,
        "kind": conn.kind,
        "url": conn.url,
        "verify_ssl": bool(conn.verify_ssl),
        "timeout": conn.timeout,
        "created_at": conn.created_at.isoformat() if conn.created_at else None,
        "updated_at": conn.updated_at.isoformat() if conn.updated_at else None,
    }
    # Secrets are only materialized for internal worker use. Public API
    # schemas (ConnectionOut) carry no token field at all.
    if include_token:
        data["token"] = decrypt_token(conn.token_encrypted)
    return data


def _validate_kind(kind: str) -> str:
    kind = (kind or "").lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    return kind


def create_connection(
    name: str,
    kind: str,
    url: str,
    token: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Create a stored connection. Raises ValueError on validation errors."""
    from aap_migration.config import AAPInstanceConfig

    kind = _validate_kind(kind)
    if not name or not name.strip():
        raise ValueError("name cannot be empty")
    if not token or not token.strip():
        raise ValueError("token cannot be empty")
    from aap_migration.utils.ssrf import validate_connection_url

    # Reuse CLI URL/token validation (https required, etc.)
    AAPInstanceConfig(url=url, token=token, verify_ssl=verify_ssl, timeout=timeout)
    # SSRF guard: always reject metadata endpoints; strict private-IP
    # blocking is opt-in via AAP_BRIDGE_SSRF_STRICT (private AAPs are legit).
    validate_connection_url(url)

    with api_session_scope(db_path) as session:
        existing = session.execute(
            select(ApiConnection).where(ApiConnection.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"Connection with name '{name}' already exists")
        conn = ApiConnection(
            id=str(uuid.uuid4()),
            name=name,
            kind=kind,
            url=url.rstrip("/"),
            token_encrypted=encrypt_token(token),
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        session.add(conn)
        session.flush()
        return _to_dict(conn)


def list_connections(kind: str | None = None, db_path: str | None = None) -> list[dict[str, Any]]:
    """List stored connections (tokens masked)."""
    with api_session_scope(db_path) as session:
        query = select(ApiConnection).order_by(ApiConnection.name)
        if kind:
            query = query.where(ApiConnection.kind == _validate_kind(kind))
        return [_to_dict(c) for c in session.execute(query).scalars().all()]


def get_connection(
    conn_id: str, *, include_token: bool = False, db_path: str | None = None
) -> dict[str, Any]:
    """Fetch a single connection. Raises KeyError if missing."""
    with api_session_scope(db_path) as session:
        conn = session.get(ApiConnection, conn_id)
        if conn is None:
            raise KeyError(f"Connection '{conn_id}' not found")
        return _to_dict(conn, include_token=include_token)


def update_connection(
    conn_id: str,
    *,
    name: str | None = None,
    url: str | None = None,
    token: str | None = None,
    verify_ssl: bool | None = None,
    timeout: int | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Update a stored connection. Raises KeyError/ValueError.

    Refuses with ValueError (mapped to HTTP 409) when any non-terminal job
    is pinned to this connection via explicit ``source_id``/``target_id``,
    submit-time ``_snapshot_*`` pins, or the active fallback -- a URL/token
    rotation in the queue window would otherwise silently switch the queued
    run to un-presented credentials (see context.verify_execution_pair).
    ``allow_pair_switch`` opt-in for chained jobs is unchanged (checked at
    submit/execution, not here).
    """
    from aap_migration.config import AAPInstanceConfig

    with api_session_scope(db_path) as session:
        conn = session.get(ApiConnection, conn_id)
        if conn is None:
            raise KeyError(f"Connection '{conn_id}' not found")
        _reject_if_connection_referenced(conn_id, session)
        if name is not None:
            if not name.strip():
                raise ValueError("name cannot be empty")
            clash = session.execute(
                select(ApiConnection).where(ApiConnection.name == name, ApiConnection.id != conn_id)
            ).scalar_one_or_none()
            if clash is not None:
                raise ValueError(f"Connection with name '{name}' already exists")
            conn.name = name
        if url is not None:
            from aap_migration.utils.ssrf import validate_connection_url

            AAPInstanceConfig(
                url=url,
                token="dummy-token-for-validation",
                verify_ssl=conn.verify_ssl if verify_ssl is None else verify_ssl,
                timeout=conn.timeout if timeout is None else timeout,
            )
            validate_connection_url(url)
            conn.url = url.rstrip("/")
        if token is not None:
            if not token.strip():
                raise ValueError("token cannot be empty")
            conn.token_encrypted = encrypt_token(token)
        if verify_ssl is not None:
            conn.verify_ssl = verify_ssl
        if timeout is not None:
            conn.timeout = timeout
        session.flush()
        return _to_dict(conn)


def _reject_if_connection_referenced(conn_id: str, session: Session) -> None:
    """Raise ConflictError when non-terminal jobs depend on *conn_id*."""
    from aap_migration.api.jobs._records import ConflictError

    try:
        from aap_migration.api.jobs import get_job_manager

        pending = get_job_manager().non_terminal_params()
    except Exception:
        return
    active = session.get(ApiActiveConfig, 1)
    active_src = active.source_id if active is not None else None
    active_tgt = active.target_id if active is not None else None
    matches = 0
    for params in pending:
        explicit = (params.get("source_id"), params.get("target_id"))
        snapshot = (params.get(SNAPSHOT_SOURCE_ID), params.get(SNAPSHOT_TARGET_ID))
        if conn_id in explicit or conn_id in snapshot:
            matches += 1
    if matches:
        raise ConflictError(
            f"Connection '{conn_id}' is referenced by {matches} queued/running job(s); "
            "wait for it to finish or cancel it before deleting."
        )
    # Active-fallback jobs (no explicit or snapshot ids) follow the
    # active pair, so deleting an active id strands them too.
    fallback_matches = sum(
        1
        for params in pending
        if not any((params.get("source_id"), params.get("target_id")))
        and not any((params.get(SNAPSHOT_SOURCE_ID), params.get(SNAPSHOT_TARGET_ID)))
    )
    if fallback_matches and conn_id in (active_src, active_tgt):
        raise ConflictError(
            f"Connection '{conn_id}' is the active connection used by "
            f"{fallback_matches} queued/running job(s); wait for it to finish "
            "or cancel it first."
        )


def delete_connection(conn_id: str, db_path: str | None = None) -> None:
    """Delete a stored connection. Raises KeyError if missing.

    Refuses with ValueError (mapped to HTTP 409) when any non-terminal
    job references the connection -- via explicit ``source_id`` /
    ``target_id`` params, submit-time ``_snapshot_*`` pins, or the active
    fallback for jobs submitted without explicit ids -- instead of letting
    the worker fail minutes later with deleted-after-submit.
    """
    with api_session_scope(db_path) as session:
        conn = session.get(ApiConnection, conn_id)
        if conn is None:
            raise KeyError(f"Connection '{conn_id}' not found")
        _reject_if_connection_referenced(conn_id, session)
        session.delete(conn)
        # Clear active references
        active = session.get(ApiActiveConfig, 1)
        if active is not None:
            if active.source_id == conn_id:
                active.source_id = None
            if active.target_id == conn_id:
                active.target_id = None


def get_active(db_path: str | None = None) -> dict[str, Any]:
    """Return the active source/target connection ids (may be None)."""
    with api_session_scope(db_path) as session:
        active = session.get(ApiActiveConfig, 1)
        if active is None:
            return {"source_id": None, "target_id": None}
        return {"source_id": active.source_id, "target_id": active.target_id}


def set_active(
    source_id: str | None = None,
    target_id: str | None = None,
    db_path: str | None = None,
    *,
    clear_source: bool = False,
    clear_target: bool = False,
) -> dict[str, Any]:
    """Set the active source/target connections. Validates kinds + existence.

    Passing ``None`` for an id is a no-op (keeps the current value) so
    partial updates do not accidentally detach the pair. To explicitly
    detach a side, pass ``clear_source=True`` / ``clear_target=True``
    (or ``DELETE /connections/active``), which sets that side to None.
    """
    with api_session_scope(db_path) as session:
        active = session.get(ApiActiveConfig, 1)
        if active is None:
            active = ApiActiveConfig(id=1, source_id=None, target_id=None)
            session.add(active)
        old_src, old_tgt = active.source_id, active.target_id
        # Guard active-pair moves while non-terminal jobs are pinned,
        # mirroring update/delete: an admin move between a submit's
        # validation and pinning (or in the queue window) would otherwise
        # silently retarget active-fallback jobs. Changing a side that
        # pending jobs reference requires draining first (409).
        src_changing = clear_source or (source_id is not None and source_id != old_src)
        tgt_changing = clear_target or (target_id is not None and target_id != old_tgt)
        if src_changing and old_src:
            _reject_if_connection_referenced(old_src, session)
        if tgt_changing and old_tgt:
            _reject_if_connection_referenced(old_tgt, session)
        if clear_source:
            active.source_id = None
        elif source_id is not None:
            conn = session.get(ApiConnection, source_id)
            if conn is None:
                raise KeyError(f"Connection '{source_id}' not found")
            if conn.kind != "source":
                raise ValueError(f"Connection '{source_id}' is not a source connection")
            active.source_id = source_id
        if clear_target:
            active.target_id = None
        elif target_id is not None:
            conn = session.get(ApiConnection, target_id)
            if conn is None:
                raise KeyError(f"Connection '{target_id}' not found")
            if conn.kind != "target":
                raise ValueError(f"Connection '{target_id}' is not a target connection")
            active.target_id = target_id
        session.flush()
        return {"source_id": active.source_id, "target_id": active.target_id}


def clear_active(
    source: bool = False,
    target: bool = False,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Detach the active source and/or target connection (explicit clear)."""
    return set_active(db_path=db_path, clear_source=source, clear_target=target)


def _resolve_source_record(
    source_id: str | None = None, db_path: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Resolve the effective source connection with token (single home).

    Applies the active-config fallback, requires a source, fetches it with
    token materialized for worker use, and validates ``kind == "source"``.
    Shared by :func:`resolve_active_pair` and the source-only branch of
    :func:`pair_fingerprint` so the missing-source/kind-mismatch errors
    cannot drift between the two.
    """
    active = get_active(db_path)
    sid = source_id or active["source_id"]
    if not sid:
        raise ValueError(
            "No source AAP configured. Create one via POST /api/v1/connections "
            "and select it via POST /api/v1/connections/active (or pass source_id)."
        )
    source = get_connection(sid, include_token=True, db_path=db_path)
    if source.get("kind") != "source":
        raise ValueError(
            f"Connection '{sid}' is a '{source.get('kind')}' connection, not a source "
            "connection; pass a source connection id."
        )
    return sid, source


def resolve_active_pair(
    source_id: str | None = None,
    target_id: str | None = None,
    db_path: str | None = None,
    need: NeedScope = "both",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Resolve (source, target) connections with tokens.

    Explicit ids win; otherwise the active config is used. With
    ``need="source"`` only the source side is required (target may be
    None); otherwise both sides are required. Raises ValueError if a
    required side is unconfigured.
    """
    sid, source = _resolve_source_record(source_id, db_path)
    if not needs_target(need):
        # Source-only callers (IAM audit/benchmark) never consume the
        # target side: return before touching it so a wrong-kind or
        # deleted active target cannot fail an unrelated read-only scan.
        return (source, None)
    active = get_active(db_path)
    tid = target_id or active["target_id"]
    if need != "source" and not tid:
        raise ValueError(
            "No target AAP configured. Create one via POST /api/v1/connections "
            "and select it via POST /api/v1/connections/active (or pass target_id)."
        )
    target = get_connection(tid, include_token=True, db_path=db_path) if tid else None
    if target is not None and target.get("kind") != "target":
        raise ValueError(
            f"Connection '{tid}' is a '{target.get('kind')}' connection, not a target "
            "connection; pass a target connection id."
        )
    return (source, target)


def pair_fingerprint(
    source_id: str | None = None,
    target_id: str | None = None,
    db_path: str | None = None,
    need: NeedScope = "both",
) -> dict[str, Any]:
    """Snapshot the effective connection pair for submit-time pinning.

    Returns resolved ids plus a fingerprint over both endpoints' URLs,
    token hashes (never token material), and TLS/timeout posture. Workers
    compare this snapshot at execution and fail fast when the pair drifted
    between submit (202) and execution, instead of silently running under
    rotated/retargeted credentials or a weakened TLS posture. Raises
    ValueError/KeyError like :func:`resolve_active_pair`.
    ``need="source"`` tolerates an unconfigured target (source-only jobs)
    and hashes only the source side, so configuring an active target while
    a source-only job waits in the FIFO queue does not change its
    fingerprint. ``need="both"`` hashes both sides.
    """
    import hashlib

    def _mix(digest: Any, record: dict[str, Any] | None) -> None:
        if record is None:
            digest.update(b"\x00")
            return
        digest.update(str(record.get("url", "")).encode())
        digest.update(b"\x00")
        digest.update(str(record.get("token", "")).encode())
        digest.update(b"\x00")
        # Posture fields: a verify_ssl true->false (or timeout) edit in the
        # queue window must fail the fingerprint like a rotation does,
        # instead of silently running the approved job with weaker TLS.
        digest.update(str(record.get("verify_ssl", "")).encode())
        digest.update(b"\x00")
        digest.update(str(record.get("timeout", "")).encode())
        digest.update(b"\x00")

    if not needs_target(need):
        # Source-only jobs never consume the target side: resolve the source
        # via the active fallback but ignore any active target entirely.
        # This keeps their fingerprint stable across unrelated target admin.
        sid, source = _resolve_source_record(source_id, db_path)
        digest = hashlib.sha256()
        _mix(digest, source)
        return {"source_id": sid, "target_id": None, "fp": digest.hexdigest()}

    source, target = resolve_active_pair(source_id, target_id, db_path, need=need)
    sid = source["id"]
    tid = target["id"] if target is not None else None
    digest = hashlib.sha256()
    for record in (source, target):
        _mix(digest, record)
    return {"source_id": sid, "target_id": tid, "fp": digest.hexdigest()}
