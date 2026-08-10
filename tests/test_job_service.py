from __future__ import annotations

import asyncio
import json

import pytest

from aap_migration.api.models import JobRecord
from aap_migration.api.services.job_service import Job, JobService


def test_job_serialization_includes_output_and_result() -> None:
    job = Job("job-1", "Example", "demo")
    job.seq_id = 7
    job.status = "completed"
    job.log_lines = ["line-1"]
    job.result = {"ok": True}

    summary = job.to_summary()
    full = job.to_dict()

    assert summary["id"] == "job-1"
    assert summary["seq_id"] == 7
    assert full["output"] == ["line-1"]
    assert full["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_start_job_marks_failures_and_persists_logs(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)

    async def failing(job, log):
        log("before boom")
        raise ValueError("boom")

    job_id = service.start_job("Failing Job", "demo", failing)
    job = service.get_job(job_id)
    assert job is not None and job._task is not None

    await job._task

    assert job.status == "failed"
    assert job.error == "ValueError: boom"
    assert any("ERROR: boom" in line for line in job.log_lines)
    assert any("ValueError: boom" in line for line in job.log_lines)

    session = session_factory()
    try:
        record = session.get(JobRecord, job_id)
        assert record is not None
        assert record.status == "failed"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_cancel_job_marks_job_cancelled(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)
    started = asyncio.Event()

    async def slow(job, log):
        started.set()
        await asyncio.sleep(1)
        return {"ok": True}

    job_id = service.start_job("Slow Job", "demo", slow)
    await started.wait()
    assert service.cancel_job(job_id) is True

    job = service.get_job(job_id)
    assert job is not None and job._task is not None
    await job._task

    assert job.status == "cancelled"
    assert job.error == "Job was cancelled"


@pytest.mark.asyncio
async def test_start_job_marks_completed_with_errors(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)

    async def partial_failure(job, log):
        log("done with errors")
        return {"total_failed": 2, "total_created": 5}

    job_id = service.start_job("Partial Job", "demo", partial_failure)
    job = service.get_job(job_id)
    assert job is not None and job._task is not None

    await job._task

    assert job.status == "completed_with_errors"
    assert job.result == {"total_failed": 2, "total_created": 5}


@pytest.mark.asyncio
async def test_start_job_marks_failed_when_phase_fails(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)

    async def phase_failure(job, log):
        return {"phases_failed": 1, "total_failed": 0}

    job_id = service.start_job("Phase Fail Job", "demo", phase_failure)
    job = service.get_job(job_id)
    assert job is not None and job._task is not None

    await job._task

    assert job.status == "failed"


def test_add_log_drops_oldest_on_full_queue() -> None:
    service = JobService()
    job = Job("job-1", "Queue Job", "demo")
    service._jobs[job.id] = job

    class BoundedQueue(asyncio.Queue):
        def __init__(self) -> None:
            super().__init__(maxsize=1)
            self.dropped: list[str] = []

        def get_nowait(self) -> str:
            dropped = super().get_nowait()
            self.dropped.append(dropped)
            return dropped

    queue = BoundedQueue()
    queue.put_nowait("old-line")
    job._subscribers.append(queue)

    service.add_log(job.id, "new-line")

    assert job.log_lines == ["new-line"]
    assert queue.dropped == ["old-line"]
    assert list(queue._queue) == ["new-line"]  # type: ignore[attr-defined]


def test_add_log_ignores_missing_jobs() -> None:
    service = JobService()
    job = Job("job-1", "Queue Job", "demo")
    service._jobs[job.id] = job

    service.add_log("missing", "ignored")
    service.add_log(job.id, "line-1")

    assert job.log_lines == ["line-1"]


def test_subscribe_unsubscribe_and_cancel_false_cases() -> None:
    service = JobService()
    job = Job("job-1", "Demo", "demo")
    service._jobs[job.id] = job

    queue = service.subscribe(job.id)

    assert queue is not None
    assert service.subscribe("missing") is None

    service.unsubscribe(job.id, queue)
    service.unsubscribe("missing", asyncio.Queue())
    assert job._subscribers == []

    assert service.cancel_job("missing") is False
    assert service.cancel_job(job.id) is False
    assert service.resume_job("missing") is False
    assert service.resume_job(job.id) is False


def test_load_job_from_db_returns_none_for_invalid_json(session_factory) -> None:
    session = session_factory()
    session.add(
        JobRecord(
            id="bad-job",
            seq_id=1,
            name="Broken",
            type="demo",
            status="completed",
            result_json="{not-json}",
            output_json=json.dumps(["ok"]),
        )
    )
    session.commit()
    session.close()

    service = JobService(db_session_factory=session_factory)
    assert service.get_job("bad-job") is None


def test_list_jobs_merges_db_history_and_memory_jobs(session_factory) -> None:
    session = session_factory()
    session.add_all(
        [
            JobRecord(id="db-1", seq_id=1, name="Persisted", type="demo", status="completed"),
            JobRecord(id="shared", seq_id=2, name="Old Shared", type="demo", status="failed"),
        ]
    )
    session.commit()
    session.close()

    service = JobService(db_session_factory=session_factory)
    in_memory = Job("shared", "Live Shared", "demo")
    in_memory.seq_id = 9
    in_memory.status = "running"
    service._jobs[in_memory.id] = in_memory

    jobs = service.list_jobs()

    shared = next(job for job in jobs if job["id"] == "shared")
    persisted = next(job for job in jobs if job["id"] == "db-1")

    assert shared["name"] == "Live Shared"
    assert shared["status"] == "running"
    assert persisted["status"] == "completed"


def test_get_logs_since_uses_memory_or_db(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)
    memory_job = Job("mem", "Memory", "demo")
    memory_job.log_lines = ["a", "b", "c"]
    service._jobs[memory_job.id] = memory_job

    session = session_factory()
    session.add(
        JobRecord(
            id="db",
            seq_id=1,
            name="Persisted",
            type="demo",
            status="completed",
            output_json=json.dumps(["x", "y", "z"]),
        )
    )
    session.commit()
    session.close()

    assert service.get_logs_since("mem", 1) == ["b", "c"]
    assert service.get_logs_since("db", 2) == ["z"]
    assert service.get_logs_since("missing") == []
