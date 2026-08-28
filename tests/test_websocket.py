from __future__ import annotations

import asyncio

import pytest
from fastapi import WebSocketDisconnect

from aap_migration.api import websocket
from aap_migration.api.services.job_service import Job, JobService


class FakeWebSocket:
    def __init__(self, *, disconnect_on_send: bool = False) -> None:
        self.accepted = False
        self.sent: list[str] = []
        self.closed: tuple[int | None, str | None] | None = None
        self.disconnect_on_send = disconnect_on_send

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        if self.disconnect_on_send:
            raise WebSocketDisconnect
        self.sent.append(text)

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = (code, reason)


@pytest.mark.asyncio
async def test_job_log_stream_closes_when_job_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websocket, "get_job_service", lambda: JobService())
    fake_ws = FakeWebSocket()

    await websocket.job_log_stream(fake_ws, "missing")

    assert fake_ws.accepted is False
    assert fake_ws.closed == (4004, "Job not found")


@pytest.mark.asyncio
async def test_job_log_stream_replays_backlog_and_closes_completed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService()
    job = Job("job-1", "Completed Job", "demo")
    job.log_lines = ["line-1", "line-2"]
    job.status = "completed"
    service._jobs[job.id] = job
    monkeypatch.setattr(websocket, "get_job_service", lambda: service)
    fake_ws = FakeWebSocket()

    await websocket.job_log_stream(fake_ws, job.id)

    assert fake_ws.accepted is True
    assert fake_ws.sent == ["line-1", "line-2"]
    assert fake_ws.closed == (None, "completed")


@pytest.mark.asyncio
async def test_job_log_stream_streams_new_lines_and_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService()
    job = Job("job-2", "Running Job", "demo")
    job.log_lines = ["existing"]
    job.status = "running"
    service._jobs[job.id] = job
    monkeypatch.setattr(websocket, "get_job_service", lambda: service)
    fake_ws = FakeWebSocket()

    task = asyncio.create_task(websocket.job_log_stream(fake_ws, job.id))
    while not job._subscribers:
        await asyncio.sleep(0)

    await job._subscribers[0].put("live")
    job.status = "failed"
    await job._subscribers[0].put(None)
    await task

    assert fake_ws.accepted is True
    assert fake_ws.sent == ["existing", "live"]
    assert fake_ws.closed == (None, "failed")
    assert job._subscribers == []


@pytest.mark.asyncio
async def test_job_log_stream_unsubscribes_on_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    service = JobService()
    job = Job("job-3", "Disconnecting Job", "demo")
    job.status = "running"
    service._jobs[job.id] = job
    monkeypatch.setattr(websocket, "get_job_service", lambda: service)
    fake_ws = FakeWebSocket(disconnect_on_send=True)

    task = asyncio.create_task(websocket.job_log_stream(fake_ws, job.id))
    while not job._subscribers:
        await asyncio.sleep(0)

    await job._subscribers[0].put("boom")
    await task

    assert job._subscribers == []
