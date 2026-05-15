"""Background job manager for the API."""

from __future__ import annotations

import asyncio
import traceback
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any


class Job:
    """Tracks a single background job."""

    __slots__ = (
        "id",
        "name",
        "type",
        "status",
        "log_lines",
        "result",
        "error",
        "created_at",
        "started_at",
        "completed_at",
        "_task",
        "_subscribers",
    )

    def __init__(self, job_id: str, name: str, job_type: str):
        self.id = job_id
        self.name = name
        self.type = job_type
        self.status = "pending"
        self.log_lines: list[str] = []
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = datetime.utcnow()
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._subscribers: list[asyncio.Queue] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": (self.started_at or self.created_at).isoformat(),
            "finished_at": self.completed_at.isoformat() if self.completed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "result": self.result,
        }


class JobService:
    """Manages background asyncio tasks with log capture and WebSocket broadcast."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def start_job(
        self,
        name: str,
        job_type: str,
        coro_factory: Callable[[Job, Callable[[str], None]], Coroutine],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
        """Start a background job.

        Args:
            name: Human-readable job name
            job_type: Category (migration, analysis, cleanup, export, etc.)
            coro_factory: Async callable(job, log_fn) that performs the work
            loop: Event loop to schedule the task on (required from sync contexts)

        Returns:
            The new job's UUID.
        """
        job_id = str(uuid.uuid4())
        job = Job(job_id, name, job_type)
        self._jobs[job_id] = job

        async def _run() -> None:
            job.status = "running"
            job.started_at = datetime.utcnow()
            try:
                result = await coro_factory(job, lambda line: self.add_log(job_id, line))
                job.result = result if isinstance(result, dict) else None
                job.status = "completed"
            except asyncio.CancelledError:
                job.status = "cancelled"
                job.error = "Job was cancelled"
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                self.add_log(job_id, f"ERROR: {exc}")
                self.add_log(job_id, traceback.format_exc())
            finally:
                job.completed_at = datetime.utcnow()
                for q in job._subscribers:
                    await q.put(None)

        target_loop = loop or asyncio.get_event_loop()
        job._task = target_loop.create_task(_run())
        return job_id

    def add_log(self, job_id: str, line: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.log_lines.append(line)
        for q in list(job._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def subscribe(self, job_id: str) -> asyncio.Queue | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        job._subscribers.append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        job = self._jobs.get(job_id)
        if job and q in job._subscribers:
            job._subscribers.remove(q)

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [
            j.to_dict()
            for j in sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        ]

    def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job._task is None:
            return False
        if job.status == "running":
            job._task.cancel()
            return True
        return False

    def get_logs_since(self, job_id: str, offset: int = 0) -> list[str]:
        job = self._jobs.get(job_id)
        if job is None:
            return []
        return job.log_lines[offset:]
