"""Connection management endpoints.

AAP endpoints (source/target URLs + tokens) are configured via API calls and
stored encrypted in the API database, instead of ``config.yaml`` / ``.env``.
Public responses never include secret tokens.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from aap_migration.api import store
from aap_migration.api.routers._common import _store_http_error, handle_store_errors
from aap_migration.api.schemas import (
    ActiveConfigIn,
    ActiveConfigOut,
    ConnectionCreate,
    ConnectionListOut,
    ConnectionOut,
    ConnectionReplace,
    ConnectionTestOut,
    ConnectionUpdate,
    DeleteConnectionOut,
)

log = logging.getLogger("aap_migration.api.connections")

router = APIRouter(tags=["connections"])


@router.post("/connections", response_model=ConnectionOut, status_code=201)
def create_connection(body: ConnectionCreate) -> ConnectionOut:
    """Store a new source/target AAP connection (token encrypted at rest)."""
    created = handle_store_errors(
        store.create_connection,
        body.name,
        body.kind,
        body.url,
        body.token,
        body.verify_ssl,
        body.timeout,
    )
    return ConnectionOut(**created)


@router.get("/connections", response_model=ConnectionListOut)
def list_connections(
    kind: Literal["source", "target"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=100000),
) -> dict:
    """List stored connections (tokens never serialized).

    One envelope always: ``{"items": [...], "total": N, "limit": L,
    "offset": O}`` matching ``GET /jobs`` and the state/checkpoint list
    endpoints, so shared pagination helpers work on day one of v1.
    Pinned via ``ConnectionListOut`` so the items envelope is part of
    the OpenAPI schema for typed clients.
    """
    try:
        connections = store.list_connections(kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    total = len(connections)
    page = connections[offset : offset + limit]
    return {
        "items": [ConnectionOut(**c).model_dump(mode="json") for c in page],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/connections/active", response_model=ActiveConfigOut)
def get_active() -> ActiveConfigOut:
    """Show which connections are currently active."""
    return ActiveConfigOut(**store.get_active())


@router.post("/connections/active", response_model=ActiveConfigOut)
def set_active(body: ActiveConfigIn) -> ActiveConfigOut:
    """Select the active source/target AAP pair used by migration jobs.

    ``source_id``/``target_id`` set a side; omitted (None) keeps the
    current value. To explicitly detach a side, pass
    ``clear_source=true`` / ``clear_target=true`` (or
    ``DELETE /connections/active``). Moving a side while queued/running
    jobs reference it is a 409 (drain or cancel first), mirroring
    update/delete.
    """
    try:
        updated = store.set_active(
            body.source_id,
            body.target_id,
            clear_source=body.clear_source,
            clear_target=body.clear_target,
        )
    except (KeyError, ValueError, IntegrityError) as exc:
        raise _store_http_error(exc) from exc
    return ActiveConfigOut(**updated)


@router.delete("/connections/active", response_model=ActiveConfigOut)
def clear_active(
    source: bool = Query(default=False, description="Detach the active source"),
    target: bool = Query(default=False, description="Detach the active target"),
) -> ActiveConfigOut:
    """Detach the active source and/or target connection.

    With no flags both sides are detached. At least one side must be
    selected: pass ``?source=true``, ``?target=true``, or both.
    Lifecycle conflicts (queued/running refs) are 409, mirroring POST.
    """
    if not source and not target:
        source = target = True
    try:
        updated = store.clear_active(source=source, target=target)
    except (KeyError, ValueError, IntegrityError) as exc:
        raise _store_http_error(exc) from exc
    return ActiveConfigOut(**updated)


@router.get("/connections/{conn_id}", response_model=ConnectionOut)
def get_connection(conn_id: str) -> ConnectionOut:
    """Fetch one stored connection (token never serialized)."""
    conn = handle_store_errors(store.get_connection, conn_id)
    return ConnectionOut(**conn)


def _update_connection(conn_id: str, body: ConnectionUpdate | ConnectionReplace) -> ConnectionOut:
    """Shared update implementation for PATCH (partial) and PUT (full)."""
    try:
        updated: dict[str, Any] = store.update_connection(
            conn_id,
            name=body.name,
            url=body.url,
            token=body.token,
            verify_ssl=body.verify_ssl,
            timeout=body.timeout,
        )
    except (KeyError, ValueError, IntegrityError) as exc:
        # Lifecycle guard (queued/running refs) is a ConflictError (409);
        # unique races are IntegrityError (409); validation errors stay
        # 400. Mapped on type in the single home.
        raise _store_http_error(exc) from exc
    # Explicit fields (not **updated): keeps mypy on the declared model
    # instead of inferring Any from the store-layer dict.
    return ConnectionOut(
        id=updated["id"],
        name=updated["name"],
        kind=updated["kind"],
        url=updated["url"],
        verify_ssl=updated["verify_ssl"],
        timeout=updated["timeout"],
        created_at=updated.get("created_at"),
        updated_at=updated.get("updated_at"),
    )


@router.patch("/connections/{conn_id}", response_model=ConnectionOut)
def patch_connection(conn_id: str, body: ConnectionUpdate) -> ConnectionOut:
    """Partially update a stored connection (omitted fields are kept)."""
    return _update_connection(conn_id, body)


@router.put("/connections/{conn_id}", response_model=ConnectionOut)
def update_connection(conn_id: str, body: ConnectionReplace) -> ConnectionOut:
    """Replace a stored connection (full-replace semantics).

    All fields are required; the stored record is replaced (``kind`` is
    immutable and kept). Use ``PATCH /connections/{conn_id}`` for partial
    updates where omitted fields are kept.
    """
    return _update_connection(conn_id, body)


@router.delete("/connections/{conn_id}", response_model=DeleteConnectionOut)
def delete_connection(conn_id: str) -> dict:
    """Delete a stored connection.

    Returns 200 with ``{"deleted_connection_id": conn_id}`` (distinct
    delete keys per resource type, P2 #13; all delete endpoints return a
    body, none uses 204). Refusing a delete that would strand
    queued/running jobs returns 409, mirroring the job-delete
    chained-reference guard.
    """
    try:
        store.delete_connection(conn_id)
    except (KeyError, ValueError, IntegrityError) as exc:
        raise _store_http_error(exc) from exc
    return {"deleted_connection_id": conn_id}


@router.post("/connections/{conn_id}/test", response_model=ConnectionTestOut)
async def test_connection(conn_id: str) -> dict:
    """Test connectivity to a stored AAP (mirrors ``config validate --check-connectivity``).

    ``reachable`` is a constant-True success marker on 200 (pinned as
    Literal[True]; failures come back as 400/502, never
    ``reachable: false``): use the status code, not the boolean, for
    failure detection.

    SSRF re-verification runs off the event loop in a bounded helper
    thread (fail-closed) so one slow hostname cannot stall all API
    traffic; the probe itself stays async with ``asyncio.wait_for``
    capped at the connection timeout (max 30s).
    """
    from aap_migration.api.security import redact_backend_error
    from aap_migration.utils.ssrf import reverify_execution_url_bounded

    conn = handle_store_errors(store.get_connection, conn_id, include_token=True)
    try:
        await asyncio.to_thread(reverify_execution_url_bounded, conn["url"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from aap_migration.client.aap_source_client import AAPSourceClient
    from aap_migration.client.aap_target_client import AAPTargetClient
    from aap_migration.config import AAPInstanceConfig

    instance = AAPInstanceConfig(
        url=conn["url"],
        token=conn["token"],
        verify_ssl=conn["verify_ssl"],
        timeout=conn["timeout"],
    )
    try:
        probe_timeout = float(conn.get("timeout", 30) or 30)
    except (TypeError, ValueError):
        probe_timeout = 30.0
    probe_timeout = max(1.0, min(probe_timeout, 30.0))
    client_cls = AAPSourceClient if conn["kind"] == "source" else AAPTargetClient
    client = client_cls(config=instance, rate_limit=10)
    try:
        try:
            await asyncio.wait_for(client.get("ping/"), timeout=probe_timeout)
            get_version = client.get_version  # type: ignore[attr-defined]
            version = await asyncio.wait_for(get_version(), timeout=probe_timeout)
        except TimeoutError as exc:
            log.exception("connectivity test timed out for %s", conn_id)
            raise HTTPException(status_code=502, detail="Connection probe timed out") from exc
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("connectivity test failed for %s", conn_id)
            raise HTTPException(status_code=502, detail=redact_backend_error(exc)) from exc
    finally:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
    return {
        "connection_id": conn_id,
        "reachable": True,
        "version": version,
        "url": conn["url"],
    }
