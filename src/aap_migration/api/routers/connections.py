from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_db
from aap_migration.api.models import Connection
from aap_migration.api.schemas import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionUpdate,
    TestResult,
)
from aap_migration.api.services.connection_service import ConnectionService

router = APIRouter(tags=["connections"])


def _mask_token(conn: Connection) -> ConnectionResponse:
    resp = ConnectionResponse.model_validate(conn)
    if conn.token:
        resp.token = "********"
    return resp


@router.post("/connections", response_model=ConnectionResponse, status_code=201)
def create_connection(data: ConnectionCreate, db: Session = Depends(get_db)) -> ConnectionResponse:
    svc = ConnectionService(db)
    conn = svc.create(data)
    return _mask_token(conn)


@router.get("/connections", response_model=list[ConnectionResponse])
def list_connections(db: Session = Depends(get_db)) -> list[ConnectionResponse]:
    svc = ConnectionService(db)
    return [_mask_token(c) for c in svc.list_all()]


@router.put("/connections/{connection_id}", response_model=ConnectionResponse)
def update_connection(
    connection_id: str, data: ConnectionUpdate, db: Session = Depends(get_db)
) -> ConnectionResponse:
    svc = ConnectionService(db)
    conn = svc.update(connection_id, data)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _mask_token(conn)


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(connection_id: str, db: Session = Depends(get_db)) -> None:
    svc = ConnectionService(db)
    if not svc.delete(connection_id):
        raise HTTPException(status_code=404, detail="Connection not found")


@router.post("/connections/{connection_id}/test", response_model=TestResult)
def test_connection(connection_id: str, db: Session = Depends(get_db)) -> TestResult:
    svc = ConnectionService(db)
    conn = svc.get(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    result = svc.test_connection(conn)
    return result
