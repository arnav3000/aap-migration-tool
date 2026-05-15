"""Connection CRUD + test endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db
from aap_migration.api.models import Connection
from aap_migration.api.schemas import (
    ConnectionCreate,
    ConnectionResponseMasked,
    ConnectionUpdate,
    TestConnectionResponse,
)
from aap_migration.api.services.connection_service import ConnectionService

router = APIRouter()


@router.post("/connections", response_model=ConnectionResponseMasked)
def create_connection(body: ConnectionCreate, db: Session = Depends(get_db)) -> Connection:
    conn = ConnectionService.create(
        db,
        name=body.name,
        url=body.url,
        token=body.token,
        role=body.role,
        verify_ssl=body.verify_ssl,
        timeout=body.timeout,
    )
    return conn


@router.get("/connections", response_model=list[ConnectionResponseMasked])
def list_connections(db: Session = Depends(get_db)) -> list[Connection]:
    return ConnectionService.list_all(db)


@router.put("/connections/{conn_id}", response_model=ConnectionResponseMasked)
def update_connection(
    conn_id: str, body: ConnectionUpdate, db: Session = Depends(get_db)
) -> Connection:
    updates = body.model_dump(exclude_unset=True)
    conn = ConnectionService.update(db, conn_id, **updates)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.delete("/connections/{conn_id}", status_code=204)
def delete_connection(conn_id: str, db: Session = Depends(get_db)) -> None:
    if not ConnectionService.delete(db, conn_id):
        raise HTTPException(status_code=404, detail="Connection not found")


@router.post("/connections/{conn_id}/test", response_model=TestConnectionResponse)
async def test_connection(conn_id: str, db: Session = Depends(get_db)) -> TestConnectionResponse:
    conn = ConnectionService.get(db, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    ok, error = await ConnectionService.test_connection(conn)

    conn.ping_status = "ok" if ok else "error"
    conn.auth_status = "ok" if ok else "error"
    db.commit()

    return TestConnectionResponse(ok=ok, error=error)
