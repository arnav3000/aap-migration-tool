"""Connection CRUD + test endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.crypto import decrypt_token
from aap_migration.api.dependencies import get_db, verify_api_token
from aap_migration.api.models import Connection
from aap_migration.api.schemas import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionUpdate,
    TestConnectionResponse,
)
from aap_migration.api.services.connection_service import ConnectionService

router = APIRouter(
    prefix="/connections", tags=["connections"], dependencies=[Depends(verify_api_token)]
)


def _to_response(conn: Connection, include_token_mask: bool = True) -> dict:
    data = ConnectionResponse.model_validate(conn).model_dump()
    if include_token_mask:
        # Never expose raw token
        data["token_masked"] = "***"
    return data


@router.post("", response_model=dict, status_code=201)
async def create_connection(body: ConnectionCreate, db: Session = Depends(get_db)) -> dict:
    # Basic URL validation same as AAPInstanceConfig but allow http for tests
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="URL must start with http:// or https://")
    conn = ConnectionService.create(
        db,
        name=body.name,
        url=body.url,
        token=body.token,
        role=body.role,
        verify_ssl=body.verify_ssl,
        timeout=body.timeout,
    )
    db.commit()
    db.refresh(conn)
    return _to_response(conn)


@router.get("", response_model=list[dict])
async def list_connections(db: Session = Depends(get_db)) -> list[dict]:
    conns = ConnectionService.list_all(db)
    return [_to_response(c) for c in conns]


@router.get("/{conn_id}", response_model=dict)
async def get_connection(conn_id: str, db: Session = Depends(get_db)) -> dict:
    conn = ConnectionService.get(db, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _to_response(conn)


@router.put("/{conn_id}", response_model=dict)
async def update_connection(
    conn_id: str, body: ConnectionUpdate, db: Session = Depends(get_db)
) -> dict:
    conn = ConnectionService.get(db, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    updates = body.model_dump(exclude_unset=True)
    if (
        "url" in updates
        and updates["url"]
        and not updates["url"].startswith(("http://", "https://"))
    ):
        raise HTTPException(status_code=422, detail="URL must start with http:// or https://")
    if "role" in updates and updates["role"] not in (None, "source", "target"):
        raise HTTPException(status_code=422, detail="role must be source or target")
    ConnectionService.update(db, conn, **updates)
    db.commit()
    db.refresh(conn)
    return _to_response(conn)


@router.delete("/{conn_id}", status_code=204)
async def delete_connection(conn_id: str, db: Session = Depends(get_db)) -> None:
    conn = ConnectionService.get(db, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    ConnectionService.delete(db, conn)
    db.commit()
    return None


@router.post("/{conn_id}/test", response_model=TestConnectionResponse)
async def test_connection(conn_id: str, db: Session = Depends(get_db)) -> TestConnectionResponse:
    conn = ConnectionService.get(db, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    ping_status, auth_status, error = await ConnectionService.test_connection(conn)
    db.commit()
    return TestConnectionResponse(ping_status=ping_status, auth_status=auth_status, error=error)


# Extra: retrieve decrypted token length hint (not value) for debugging — masked
@router.get("/{conn_id}/token-hint", response_model=dict)
async def token_hint(conn_id: str, db: Session = Depends(get_db)) -> dict:
    conn = ConnectionService.get(db, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    try:
        tok = decrypt_token(conn.token)
        return {"length": len(tok), "prefix": tok[:4] + "***" if len(tok) >= 4 else "***"}
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
