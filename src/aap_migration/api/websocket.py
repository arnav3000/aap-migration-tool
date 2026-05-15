"""WebSocket endpoint for streaming job logs."""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aap_migration.api.dependencies import get_job_service

router = APIRouter()


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

    if job.status in ("completed", "failed", "cancelled"):
        await websocket.close(reason=job.status)
        return

    q = job_service.subscribe(job_id)
    if q is None:
        await websocket.close(reason="subscribe_failed")
        return

    try:
        while True:
            line = await q.get()
            if line is None:
                await websocket.close(reason=job.status)
                break
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        job_service.unsubscribe(job_id, q)
