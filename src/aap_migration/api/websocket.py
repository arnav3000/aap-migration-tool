"""WebSocket endpoint for streaming job logs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aap_migration.api.dependencies import get_job_service

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/jobs/{job_id}/logs")
async def job_log_stream(websocket: WebSocket, job_id: str) -> None:
    job_service = get_job_service()
    job = job_service.get_job(job_id)
    if not job:
        await websocket.close(code=4404, reason="Job not found")
        return
    await websocket.accept()
    queue = job_service.subscribe(job_id)
    if queue is None:
        await websocket.close(code=4404, reason="subscribe_failed")
        return
    try:
        # subscribe() already replays log_lines into queue + enqueues __JOB_DONE__ if finished
        # so just drain queue
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=30.0)
                if line == "__JOB_DONE__":
                    await websocket.send_text(line)
                    break
                await websocket.send_text(line)
            except TimeoutError:
                # keepalive ping
                try:
                    await websocket.send_text("__PING__")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        job_service.unsubscribe(job_id, queue)
        try:
            await websocket.close()
        except Exception:
            pass
