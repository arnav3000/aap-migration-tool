from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from aap_migration.api.dependencies import get_app_state, get_db
from aap_migration.api.schemas import JobCreatedResponse
from aap_migration.api.services.connection_service import ConnectionService

router = APIRouter(tags=["operations"])


@router.post("/connections/{connection_id}/cleanup", response_model=JobCreatedResponse)
def run_cleanup(connection_id: str, db: Session = Depends(get_db)) -> JobCreatedResponse:
    svc = ConnectionService(db)
    conn = svc.get(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    state = get_app_state()
    from aap_migration.api.services.operation_service import OperationService

    op_svc = OperationService(state.job_service, state.db_session_factory, state.loop)
    job_id = op_svc.start_cleanup(conn)
    return JobCreatedResponse(job_id=job_id)


@router.post("/connections/{connection_id}/export", response_model=JobCreatedResponse)
def run_export(connection_id: str, db: Session = Depends(get_db)) -> JobCreatedResponse:
    svc = ConnectionService(db)
    conn = svc.get(connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    state = get_app_state()
    from aap_migration.api.services.operation_service import OperationService

    op_svc = OperationService(state.job_service, state.db_session_factory, state.loop)
    job_id = op_svc.start_export(conn)
    return JobCreatedResponse(job_id=job_id)
