"""Background job manager for the API with DB persistence."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session, sessionmaker

from aap_migration.api.models import JobRecord


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    RESUMED = "resumed"


class Job:
    """Tracks a single background job (in-memory for live streaming)."""

    def __init__(
        self,
        job_id: str,
        seq_id: int,
        job_type: str,
        name: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.seq_id = seq_id
        self.type = job_type
        self.name = name
        self.status: JobStatus = JobStatus.PENDING
        self.created_at: datetime = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.output: dict[str, Any] | None = None
        self.log_lines: list[str] = []
        self._task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._resume_event: asyncio.Event = asyncio.Event()
        # For resume support
        self._paused_plan_id: str | None = None
        self._paused_phase_id: str | None = None

    async def wait_for_resume(self) -> None:
        """Block until resume_job() is called, then reset the event."""
        await self._resume_event.wait()
        self._resume_event.clear()

    def append_log(self, line: str) -> None:
        self.log_lines.append(line)
        for q in list(self._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "seq_id": self.seq_id,
            "type": self.type,
            "status": self.status.value,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        d = self.to_summary()
        d.update({"result": self.result, "output": self.output, "log_lines": list(self.log_lines)})
        return d


class JobService:
    """Manages background asyncio tasks with log capture and DB persistence."""

    def __init__(
        self, db_session_factory: sessionmaker, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        self.db_session_factory: sessionmaker = db_session_factory
        self.loop = loop or asyncio.get_event_loop()
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock() if loop else None
        self._seq_lock = asyncio.Lock()

    def _next_seq_id(self, session: Session) -> int:
        row = session.query(JobRecord).order_by(desc(JobRecord.seq_id)).first()
        if row and row.seq_id is not None:
            return int(row.seq_id) + 1
        # Count existing for fresh DB
        cnt = session.query(JobRecord).count()
        return cnt + 1

    def persist_job(self, job: Job) -> None:
        """Write job state to DB (called on completion/failure/cancel)."""
        try:
            session: Session = self.db_session_factory()
            try:
                rec = session.get(JobRecord, job.job_id)
                if rec is None:
                    rec = JobRecord(id=job.job_id, seq_id=job.seq_id, type=job.type, name=job.name)
                    session.add(rec)
                rec.status = job.status.value
                rec.started_at = job.started_at
                rec.completed_at = job.completed_at
                rec.error = job.error
                rec.result_json = json.dumps(job.result) if job.result is not None else None
                rec.output_json = json.dumps(job.output) if job.output is not None else None
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        except Exception:
            # Persistence is best-effort
            pass

    def create_and_persist_initial(self, job: Job) -> None:
        """Create the initial DB row so foreign keys can reference the job immediately."""
        try:
            session: Session = self.db_session_factory()
            try:
                rec = JobRecord(
                    id=job.job_id,
                    seq_id=job.seq_id,
                    type=job.type,
                    status=job.status.value,
                    name=job.name,
                    started_at=job.started_at,
                )
                session.add(rec)
                session.commit()
            except Exception:
                session.rollback()
            finally:
                session.close()
        except Exception:
            pass

    async def start_job(
        self,
        job_type: str,
        coro_factory: Callable[[Callable[[str], None]], Coroutine[Any, Any, Any]],
        name: str | None = None,
    ) -> Job:
        """Start a background job.

        coro_factory receives an append_log callable and should return result dict.
        """
        job_id = str(uuid.uuid4())
        # Allocate seq_id synchronously
        session: Session = self.db_session_factory()
        try:
            seq_id = self._next_seq_id(session)
        finally:
            session.close()

        job = Job(job_id=job_id, seq_id=seq_id, job_type=job_type, name=name)
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        self._jobs[job_id] = job
        self.create_and_persist_initial(job)

        async def _run() -> None:
            try:

                def append_log(line: str) -> None:
                    job.append_log(line)

                result = await coro_factory(append_log)
                # coro_factory may return dict or None
                if isinstance(result, dict):
                    job.result = result
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.now(UTC)
            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                job.error = "cancelled"
                job.completed_at = datetime.now(UTC)
            except Exception as e:
                job.status = JobStatus.FAILED
                job.error = str(e)
                job.completed_at = datetime.now(UTC)
            finally:
                self.persist_job(job)
                # Notify subscribers of completion
                for q in list(job._subscribers):
                    try:
                        q.put_nowait("__JOB_DONE__")
                    except Exception:
                        pass

        job._task = asyncio.create_task(_run())
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        # Prefer DB for persistence across restarts, but merge in-memory for live
        try:
            session: Session = self.db_session_factory()
            try:
                rows = session.query(JobRecord).order_by(desc(JobRecord.seq_id)).all()
                out: list[dict[str, Any]] = []
                for r in rows:
                    j = self._jobs.get(r.id)
                    if j is not None:
                        out.append(j.to_summary())
                    else:
                        out.append(
                            {
                                "id": r.id,
                                "seq_id": r.seq_id,
                                "type": r.type,
                                "status": r.status,
                                "name": r.name,
                                "created_at": r.created_at.isoformat() if r.created_at else None,
                                "started_at": r.started_at.isoformat() if r.started_at else None,
                                "completed_at": (
                                    r.completed_at.isoformat() if r.completed_at else None
                                ),
                                "error": r.error,
                            }
                        )
                return out
            finally:
                session.close()
        except Exception:
            return [j.to_summary() for j in self._jobs.values()]

    def get_job_dict(self, job_id: str) -> dict[str, Any] | None:
        j = self._jobs.get(job_id)
        if j is not None:
            return j.to_dict()
        # Fallback to DB
        try:
            session: Session = self.db_session_factory()
            try:
                rec: JobRecord | None = session.get(JobRecord, job_id)
                if rec is None:
                    return None
                return {
                    "id": rec.id,
                    "seq_id": rec.seq_id,
                    "type": rec.type,
                    "status": rec.status,
                    "name": rec.name,
                    "created_at": rec.created_at.isoformat() if rec.created_at else None,
                    "started_at": rec.started_at.isoformat() if rec.started_at else None,
                    "completed_at": rec.completed_at.isoformat() if rec.completed_at else None,
                    "error": rec.error,
                    "result": json.loads(rec.result_json) if rec.result_json else None,
                    "output": json.loads(rec.output_json) if rec.output_json else None,
                }
            finally:
                session.close()
        except Exception:
            return None

    async def cancel_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            # Try DB: mark cancelled if still pending/running
            try:
                session: Session = self.db_session_factory()
                try:
                    rec = session.get(JobRecord, job_id)
                    if rec and rec.status in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
                        rec.status = JobStatus.CANCELLED.value
                        rec.completed_at = datetime.now(UTC)
                        session.commit()
                        return True
                finally:
                    session.close()
            except Exception:
                pass
            return False
        if job._task and not job._task.done():
            job._task.cancel()
            try:
                await job._task
            except asyncio.CancelledError:
                pass
            return True
        return False

    async def resume_job(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status != JobStatus.WAITING_FOR_INPUT:
            return False
        job._resume_event.set()
        return True

    def subscribe(self, job_id: str) -> asyncio.Queue[str] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        job._subscribers.add(q)
        # Replay existing logs
        for line in job.log_lines:
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                break
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            try:
                q.put_nowait("__JOB_DONE__")
            except Exception:
                pass
        return q

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[str]) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job._subscribers.discard(queue)

    def recover_stale_jobs(self) -> None:
        """Mark any jobs left in running state from previous process as failed."""
        try:
            session: Session = self.db_session_factory()
            try:
                rows = (
                    session.query(JobRecord)
                    .filter(JobRecord.status == JobStatus.RUNNING.value)
                    .all()
                )
                for r in rows:
                    r.status = JobStatus.FAILED.value
                    r.error = "process restarted - job marked failed"
                    r.completed_at = datetime.now(UTC)
                if rows:
                    session.commit()
            finally:
                session.close()
        except Exception:
            pass
