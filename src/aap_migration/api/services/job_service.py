"""Background job manager for the API with DB persistence."""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session


class Job:
    """Tracks a single background job (in-memory for live streaming)."""

    __slots__ = (
        "id",
        "seq_id",
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
        "_html_report",
    )

    def __init__(self, job_id: str, name: str, job_type: str):
        self.id = job_id
        self.seq_id: int | None = None
        self.name = name
        self.type = job_type
        self.status = "pending"
        self.log_lines: list[str] = []
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.created_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._subscribers: list[asyncio.Queue] = []
        self._html_report: str | None = None

    def to_summary(self) -> dict[str, Any]:
        """Lightweight dict for job listing (no output/result)."""
        return {
            "id": self.id,
            "seq_id": self.seq_id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": (self.started_at or self.created_at).isoformat(),
            "finished_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_summary(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "output": self.log_lines,
        }


class JobService:
    """Manages background asyncio tasks with log capture, WebSocket broadcast, and DB persistence."""

    def __init__(self, db_session_factory: Any = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._db_session_factory = db_session_factory

    def _persist_job(self, job: Job) -> None:
        """Write job state to DB (called on completion/failure/cancel)."""
        if self._db_session_factory is None:
            return
        try:
            from aap_migration.api.models import JobRecord

            session: Session = self._db_session_factory()
            try:
                record = session.get(JobRecord, job.id)
                if record is None:
                    next_seq = self._next_seq_id(session)
                    record = JobRecord(
                        id=job.id,
                        seq_id=next_seq,
                        name=job.name,
                        type=job.type,
                    )
                    session.add(record)
                record.status = job.status
                record.error = job.error
                record.started_at = job.started_at
                record.completed_at = job.completed_at
                record.result_json = json.dumps(job.result, default=str) if job.result else None
                record.output_json = json.dumps(job.log_lines)
                session.commit()
                session.refresh(record)
                job.seq_id = record.seq_id
            except Exception:
                session.rollback()
            finally:
                session.close()
        except Exception:  # nosec B110
            pass

    @staticmethod
    def _next_seq_id(session: Session) -> int:
        from aap_migration.api.models import JobRecord

        result = session.query(JobRecord.seq_id).order_by(JobRecord.seq_id.desc()).first()
        return (result[0] + 1) if result else 1

    def _persist_job_initial(self, job: Job) -> None:
        """Create the initial DB row so foreign keys can reference the job immediately."""
        if self._db_session_factory is None:
            return
        try:
            from aap_migration.api.models import JobRecord

            session: Session = self._db_session_factory()
            try:
                next_seq = self._next_seq_id(session)
                record = JobRecord(
                    id=job.id,
                    seq_id=next_seq,
                    name=job.name,
                    type=job.type,
                    status=job.status,
                    started_at=job.started_at,
                )
                session.add(record)
                session.commit()
                job.seq_id = record.seq_id
            except Exception:
                session.rollback()
            finally:
                session.close()
        except Exception:  # nosec B110
            pass

    def start_job(
        self,
        name: str,
        job_type: str,
        coro_factory: Callable[[Job, Callable[[str], None]], Coroutine],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        job = Job(job_id, name, job_type)
        job.status = "running"
        job.started_at = datetime.now(UTC)
        self._jobs[job_id] = job
        self._persist_job_initial(job)

        async def _run() -> None:
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
                job.completed_at = datetime.now(UTC)
                self._persist_job(job)
                for q in job._subscribers:
                    await q.put(None)

        target_loop = loop or asyncio.get_running_loop()
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
        """Get job from memory first, then fall back to DB."""
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        return self._load_job_from_db(job_id)

    def _load_job_from_db(self, job_id: str) -> Job | None:
        """Reconstruct a Job from the DB for historical viewing."""
        if self._db_session_factory is None:
            return None
        try:
            from aap_migration.api.models import JobRecord

            session: Session = self._db_session_factory()
            try:
                record = session.get(JobRecord, job_id)
                if record is None:
                    return None
                job = Job(record.id, record.name, record.type)
                job.seq_id = record.seq_id
                job.status = record.status
                job.error = record.error
                job.created_at = record.created_at
                job.started_at = record.started_at
                job.completed_at = record.completed_at
                if record.result_json:
                    job.result = json.loads(record.result_json)
                if record.output_json:
                    job.log_lines = json.loads(record.output_json)
                return job
            finally:
                session.close()
        except Exception:
            return None

    def list_jobs(self) -> list[dict[str, Any]]:
        """List jobs: active in-memory jobs merged with DB history."""
        jobs_map: dict[str, dict[str, Any]] = {}

        if self._db_session_factory is not None:
            try:
                from aap_migration.api.models import JobRecord

                session: Session = self._db_session_factory()
                try:
                    records = (
                        session.query(JobRecord)
                        .order_by(JobRecord.created_at.desc())
                        .limit(200)
                        .all()
                    )
                    for r in records:
                        jobs_map[r.id] = {
                            "id": r.id,
                            "seq_id": r.seq_id,
                            "name": r.name,
                            "type": r.type,
                            "status": r.status,
                            "created_at": r.created_at.isoformat() if r.created_at else "",
                            "started_at": (r.started_at or r.created_at).isoformat()
                            if r.created_at
                            else "",
                            "finished_at": r.completed_at.isoformat() if r.completed_at else None,
                            "error": r.error,
                        }
                finally:
                    session.close()
            except Exception:  # nosec B110
                pass

        for job in self._jobs.values():
            jobs_map[job.id] = job.to_summary()

        return sorted(jobs_map.values(), key=lambda j: j.get("created_at", ""), reverse=True)

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
            loaded = self._load_job_from_db(job_id)
            if loaded is None:
                return []
            return loaded.log_lines[offset:]
        return job.log_lines[offset:]
