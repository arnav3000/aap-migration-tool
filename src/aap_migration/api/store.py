"""CRUD operations for API-managed AAP connections."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from aap_migration.api._errors import ConflictError
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
SNAPSHOT_STABLE = "_snapshot_stable"
SNAPSHOT_NEED = "_snapshot_need"
SNAPSHOT_FERNET_FP = "_snapshot_fernet_fp"
SNAPSHOT_TARGET_STABLE = "_snapshot_target_stable"

# Chaining/override param keys (P3 #18): single home beside SNAPSHOT_*
# so a typo fails at import/attribute time instead of silently
# disabling pair-switch opt-in, chaining, or force.
PARAM_JOB_ID = "job_id"
PARAM_ALLOW_PAIR_SWITCH = "allow_pair_switch"
PARAM_FORCE = "force"
PARAM_SOURCE_ID = "source_id"
PARAM_TARGET_ID = "target_id"
PARAM_VERIFY_SSL = "verify_ssl"
# Per-side stable key for the target (write) side only (P1 #4): the
# full-pair fp/stable mix both endpoints, so a resubmission from a
# different source to the same target slips the pair fence while a
# timed-out orphan is still writing to that target. The target-side key
# lets fences gate on target overlap alone; source-only (read-only)
# jobs carry no target key and never block on source overlap.


def _normalize_url_for_stable(url: str | None) -> str:
    """Normalize a connection URL for stable pair identity (no secrets).

    Lowercases scheme/host, strips trailing slashes: token rotations and
    TLS/timeout edits keep the same stable key (same physical controllers),
    while a URL retarget to different controllers yields a different key.
    """
    if not url:
        return ""
    try:
        normalized = str(url).strip().rstrip("/")
        # Lowercase scheme + host, preserve path case (AAP paths are case-sensitive).
        if "://" in normalized:
            scheme, rest = normalized.split("://", 1)
            if "/" in rest:
                host, path = rest.split("/", 1)
                return f"{scheme.lower()}://{host.lower()}/{path}"
            return f"{scheme.lower()}://{rest.lower()}"
        return normalized.lower()
    except Exception:
        return str(url)


# Job types that never touch connections (no selectors, no snapshot pins
# by construction): they must never veto connection admin.
CONNECTIONLESS_JOB_TYPES = frozenset({"iam-report", "state-export"})


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


@dataclass(frozen=True)
class ConnectionScope:
    """Single value object for connection-scoped resolution (P2 #16).

    Every connection-scoped entry point takes this instead of threading
    ``(source_id, target_id, db_path, need)`` through each signature, so a
    new scope dimension needs one field here, not synchronized edits
    across store and context signatures.
    """

    source_id: str | None = None
    target_id: str | None = None
    db_path: str | None = None
    need: NeedScope = "both"


log = logging.getLogger("aap_migration.api.store")


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


def _pending_refs() -> list[dict[str, Any]]:
    """Non-terminal ``{"job_type", "params"}`` refs, manager-unavailable safe.

    When the job manager cannot be reached (not yet constructed, torn
    down, or broken), there is no queue state to veto against: return []
    and log a warning so the fail-open is visible in server logs instead
    of a silent pass. Env-key rotations (AAP_BRIDGE_API_KEY) are never
    covered by these refs -- drain the queue before rotating keys.
    """
    try:
        from aap_migration.api.jobs import get_job_manager

        return get_job_manager().non_terminal_refs()
    except Exception as exc:
        log.warning(
            "job-manager refs unavailable; connection vetoes see empty queue: %s",
            type(exc).__name__,
        )
        return []


def _is_connectionless(ref: dict[str, Any]) -> bool:
    """True when the ref is a connectionless job by construction.

    Connection-bearing submits are always pinned with snapshot ids
    (``submit_chained`` raises before enqueue otherwise), so a job type in
    ``CONNECTIONLESS_JOB_TYPES`` plus id-less params is connectionless by
    construction. Unknown types with id-less params are treated as legacy
    active-followers (veto) rather than silently skipped.
    """
    if ref.get("job_type") in CONNECTIONLESS_JOB_TYPES:
        return True
    params = ref.get("params") or {}
    if params.get(SNAPSHOT_NEED) == "none":
        return True
    return False


def _reject_if_connection_referenced(
    conn_id: str,
    session: Session,
    pending_refs: Callable[[], list[dict[str, Any]]] | None = None,
) -> None:
    """Raise ConflictError when non-terminal jobs depend on *conn_id*.

    *pending_refs* injects the queue-state provider (default: the
    process-wide job manager via :func:`_pending_refs`); passing it
    keeps this guard runnable without constructing the singleton.
    """
    refs = pending_refs() if pending_refs is not None else _pending_refs()
    active = session.get(ApiActiveConfig, 1)
    active_src = active.source_id if active is not None else None
    active_tgt = active.target_id if active is not None else None
    matches = 0
    for ref in refs:
        if _is_connectionless(ref):
            continue
        params = ref.get("params") or {}
        explicit = (params.get("source_id"), params.get("target_id"))
        snapshot = (params.get(SNAPSHOT_SOURCE_ID), params.get(SNAPSHOT_TARGET_ID))
        if conn_id in explicit or conn_id in snapshot:
            matches += 1
    if matches:
        raise ConflictError(
            f"Connection '{conn_id}' is referenced by {matches} queued/running job(s); "
            "wait for it to finish or cancel it before deleting."
        )
    # Legacy active-followers (no explicit or snapshot ids at all, not
    # connectionless): they resolve via the active pair at execution, so
    # deleting an active id strands them too.
    fallback_matches = sum(
        1
        for ref in refs
        if not _is_connectionless(ref)
        and not any(
            (
                (ref.get("params") or {}).get("source_id"),
                (ref.get("params") or {}).get("target_id"),
            )
        )
        and not any(
            (
                (ref.get("params") or {}).get(SNAPSHOT_SOURCE_ID),
                (ref.get("params") or {}).get(SNAPSHOT_TARGET_ID),
            )
        )
    )
    if fallback_matches and conn_id in (active_src, active_tgt):
        raise ConflictError(
            f"Connection '{conn_id}' is the active connection used by "
            f"{fallback_matches} queued/running job(s); wait for it to finish "
            "or cancel it first."
        )


def _reject_if_active_referenced(
    conn_id: str,
    session: Session,
    pending_refs: Callable[[], list[dict[str, Any]]] | None = None,
) -> None:
    """Veto an active-pair move only for active-following jobs.

    Unlike :func:`_reject_if_connection_referenced` (update/delete: any
    explicit or snapshot pin vetoes), moving the active pair only affects
    jobs that resolve via the active config -- those with no explicit id
    for the moving side. Jobs with explicit ids (even pinned) and
    connectionless jobs never consult the active pair, so unrelated
    explicit-id work must not veto an active-pair move.
    """
    refs = pending_refs() if pending_refs is not None else _pending_refs()
    active = session.get(ApiActiveConfig, 1)
    active_src = active.source_id if active is not None else None
    active_tgt = active.target_id if active is not None else None
    side: str | None = None
    if conn_id == active_src:
        side = "source"
    elif conn_id == active_tgt:
        side = "target"
    else:
        return
    snap_key = SNAPSHOT_SOURCE_ID if side == "source" else SNAPSHOT_TARGET_ID
    explicit_key = "source_id" if side == "source" else "target_id"
    followers = 0
    for ref in refs:
        if _is_connectionless(ref):
            continue
        params = ref.get("params") or {}
        # Source-only jobs never consume the target side (and vice versa
        # for any future target-only scope): they must not veto a move of
        # a side they never resolve. Legacy refs without a need key default
        # to 'both' and conservatively still veto.
        need = params.get(SNAPSHOT_NEED) or "both"
        if side == "target" and need == "source":
            continue
        if side == "source" and need == "target":
            continue
        if params.get(explicit_key):
            continue  # explicit id: does not follow the active pair
        snap = params.get(snap_key)
        if snap is not None:
            if snap == conn_id:
                followers += 1
        else:
            followers += 1  # legacy pinless follower of the active pair
    if followers:
        raise ConflictError(
            f"Connection '{conn_id}' is the active {side} connection used by "
            f"{followers} queued/running job(s); wait for it to finish "
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
            _reject_if_active_referenced(old_src, session)
        if tgt_changing and old_tgt:
            _reject_if_active_referenced(old_tgt, session)
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
    scope: ConnectionScope,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve (source, target) connections with tokens.

    Explicit ids win; otherwise the active config is used. With
    ``need="source"`` only the source side is required (target may be
    None); otherwise both sides are required. Raises ValueError if a
    required side is unconfigured.

    With ``need="none"`` (connectionless jobs) no side is consumed and
    ``(None, None)`` is returned without touching the store, so a
    connectionless submit never fails for want of a source and later
    source admin never vetoes it (P2 #17). Connectionless callers must
    not dereference the pair.
    """
    if not needs_connections(scope.need):
        return (None, None)
    sid, source = _resolve_source_record(scope.source_id, scope.db_path)
    if not needs_target(scope.need):
        # Source-only callers (IAM audit/benchmark) never consume the
        # target side: return before touching it so a wrong-kind or
        # deleted active target cannot fail an unrelated read-only scan.
        return (source, None)
    active = get_active(scope.db_path)
    tid = scope.target_id or active["target_id"]
    if scope.need != "source" and not tid:
        raise ValueError(
            "No target AAP configured. Create one via POST /api/v1/connections "
            "and select it via POST /api/v1/connections/active (or pass target_id)."
        )
    target = get_connection(tid, include_token=True, db_path=scope.db_path) if tid else None
    if target is not None and target.get("kind") != "target":
        raise ValueError(
            f"Connection '{tid}' is a '{target.get('kind')}' connection, not a target "
            "connection; pass a target connection id."
        )
    return (source, target)


def stored_posture(scope: ConnectionScope) -> dict[str, dict[str, Any]]:
    """Stored verify_ssl/timeout per consumed side (P1 #3, no tokens).

    Returns ``{"source": {...}, "target": {...}}`` with only the posture
    fields (no token materialization, so no decryption cost): the TLS
    posture veto compares per-job overrides against these without a full
    pair resolution. Raises KeyError/ValueError like the resolvers above.
    """
    if not needs_connections(scope.need):
        return {}
    active = get_active(scope.db_path)
    out: dict[str, dict[str, Any]] = {}
    sid = scope.source_id or active["source_id"]
    if not sid:
        raise ValueError(
            "No source AAP configured. Create one via POST /api/v1/connections "
            "and select it via POST /api/v1/connections/active (or pass source_id)."
        )
    record = get_connection(sid, db_path=scope.db_path)
    out["source"] = {"verify_ssl": bool(record.get("verify_ssl", True))}
    if needs_target(scope.need):
        tid = scope.target_id or active["target_id"]
        if not tid:
            raise ValueError(
                "No target AAP configured. Create one via POST /api/v1/connections "
                "and select it via POST /api/v1/connections/active (or pass target_id)."
            )
        target = get_connection(tid, db_path=scope.db_path)
        out["target"] = {"verify_ssl": bool(target.get("verify_ssl", True))}
    return out


def _mix_record(digest: Any, record: dict[str, Any] | None) -> None:
    """Mix a resolved connection record into *digest* (no secrets in clear).

    The URL is canonicalized exactly like the stable key (P2 #18), so a
    case-only URL edit to the same physical controller no longer drifts
    the credential fingerprint and fails queued jobs as a rotation.
    """
    if record is None:
        digest.update(b"\x00")
        return
    digest.update(_normalize_url_for_stable(record.get("url")).encode())
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


def _mix_stable(digest: Any, record: dict[str, Any] | None) -> None:
    if record is None:
        digest.update(b"\x00")
        return
    digest.update(_normalize_url_for_stable(record.get("url")).encode())
    digest.update(b"\x00")


def _side_stable(record: dict[str, Any] | None) -> str | None:
    """Stable per-side key for one endpoint's normalized URL (P1 #4).

    Returns None when there is no record, so source-only and
    connectionless fingerprints carry no target-side key and their
    jobs never block on target overlap.
    """
    if record is None:
        return None
    digest = hashlib.sha256()
    digest.update(_normalize_url_for_stable(record.get("url")).encode())
    digest.update(b"\x00target")
    return digest.hexdigest()


def fingerprint_of_records(
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
    need: NeedScope,
) -> dict[str, Any]:
    """Fingerprint an already-resolved pair without re-reading the store.

    Single home for digest construction shared by :func:`pair_fingerprint`
    (resolves, then fingerprints) and execution-time re-verification, so a
    rotation landing between verify and resolve cannot slip through a
    second resolution (see context.setup_chained). Connectionless
    (``need="none"``) fingerprints are source-independent constants.
    """
    if not needs_connections(need):
        digest = hashlib.sha256(b"connectionless\x00")
        stable = hashlib.sha256(b"connectionless\x00")
        return {
            "source_id": None,
            "target_id": None,
            "fp": digest.hexdigest(),
            "stable": stable.hexdigest(),
            "target_stable": None,
        }
    if not needs_target(need):
        digest = hashlib.sha256()
        _mix_record(digest, source)
        stable = hashlib.sha256()
        _mix_stable(stable, source)
        stable.update(b"\x00source")
        return {
            "source_id": source.get("id") if source else None,
            "target_id": None,
            "fp": digest.hexdigest(),
            "stable": stable.hexdigest(),
            "target_stable": None,
        }
    digest = hashlib.sha256()
    for record in (source, target):
        _mix_record(digest, record)
    stable = hashlib.sha256()
    for record in (source, target):
        _mix_stable(stable, record)
    return {
        "source_id": source.get("id") if source else None,
        "target_id": target.get("id") if target else None,
        "fp": digest.hexdigest(),
        "stable": stable.hexdigest(),
        "target_stable": _side_stable(target),
    }


def pair_fingerprint(scope: ConnectionScope) -> dict[str, Any]:
    """Snapshot the effective connection pair for submit-time pinning.

    Returns resolved ids plus a fingerprint over both endpoints' URLs,
    token hashes (never token material), and TLS/timeout posture, plus a
    stable key over normalized URLs only (no token/posture) for fencing.
    Workers compare the fingerprint at execution and fail fast when the
    pair drifted between submit (202) and execution, instead of silently
    running under rotated/retargeted credentials or a weakened TLS posture.
    Fences use the stable key so a token rotation or TLS/timeout edit to
    the same physical controllers stays fenced until orphans drain, while
    a URL retarget to different controllers correctly unfences. Raises
    ValueError/KeyError like :func:`resolve_active_pair`.
    ``need="source"`` tolerates an unconfigured target (source-only jobs)
    and hashes only the source side, so configuring an active target while
    a source-only job waits in the FIFO queue does not change its
    fingerprint. ``need="both"`` hashes both sides. ``need="none"``
    returns source-independent constant pins (P2 #17).
    """
    if not needs_connections(scope.need):
        return fingerprint_of_records(None, None, scope.need)
    if not needs_target(scope.need):
        # Source-only jobs never consume the target side: resolve the source
        # via the active fallback but ignore any active target entirely.
        # This keeps their fingerprint stable across unrelated target admin.
        _, src_record = _resolve_source_record(scope.source_id, scope.db_path)
        return fingerprint_of_records(src_record, None, scope.need)

    source, target = resolve_active_pair(scope)
    return fingerprint_of_records(source, target, scope.need)
