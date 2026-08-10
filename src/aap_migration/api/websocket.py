"""WebSocket endpoint for streaming job logs."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aap_migration.api.dependencies import get_job_service
from aap_migration.api.services.job_service import JobStatus

router = APIRouter()

_LOG_IDLE_TIMEOUT_SECONDS = 300.0
_TERMINAL_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_ERRORS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }
)
_ACTIVE_STATUSES = frozenset({JobStatus.RUNNING, JobStatus.WAITING_FOR_INPUT})


@router.websocket("/ws/jobs/{job_id}/logs")
async def job_log_stream(websocket: WebSocket, job_id: str) -> None:
    job_service = get_job_service()
    job = job_service.get_job(job_id)
    if job is None:
        await websocket.close(code=4004, reason="Job not found")
        return

    await websocket.accept()

    for line in job.log_lines:
        await websocket.send_text(line)

    if job.status in _TERMINAL_STATUSES:
        await websocket.close(reason=job.status)
        return

    q = job_service.subscribe(job_id)
    if q is None:
        await websocket.close(reason="subscribe_failed")
        return

    try:
        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=_LOG_IDLE_TIMEOUT_SECONDS)
            except TimeoutError:
                current = job_service.get_job(job_id)
                if current is None or current.status not in _ACTIVE_STATUSES:
                    await websocket.close(reason="job_finished")
                    break
                await websocket.send_json({"_event": "heartbeat"})
                continue

            if line is None:
                await websocket.close(reason=job.status)
                break
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        job_service.unsubscribe(job_id, q)
