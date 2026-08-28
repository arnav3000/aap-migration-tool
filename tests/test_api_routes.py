from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aap_migration.api import dependencies
from aap_migration.api.models import JobRecord
from aap_migration.api.routers import jobs
from aap_migration.api.services.job_service import Job, JobService


def test_connection_crud_routes(client) -> None:
    payload = {
        "name": "Source AAP",
        "url": "https://source.example.com",
        "token": "token-1",
        "type": "awx",
        "role": "source",
        "verify_ssl": False,
        "timeout": 50,
    }

    created = client.post("/api/connections", json=payload)
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "Source AAP"
    assert "token" not in body

    listed = client.get("/api/connections")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    updated = client.put(
        f"/api/connections/{body['id']}",
        json={"token": "token-2", "timeout": 75, "name": "Updated Source"},
    )
    assert updated.status_code == 200
    assert updated.json()["timeout"] == 75
    assert updated.json()["name"] == "Updated Source"

    deleted = client.delete(f"/api/connections/{body['id']}")
    assert deleted.status_code == 204
    assert client.get("/api/connections").json() == []


def test_connection_test_route_updates_statuses(client, monkeypatch: pytest.MonkeyPatch) -> None:
    created = client.post(
        "/api/connections",
        json={
            "name": "Target AAP",
            "url": "https://target.example.com",
            "token": "token-1",
            "type": "aap",
            "role": "target",
        },
    )
    conn_id = created.json()["id"]

    async def fake_test_connection(conn) -> tuple[bool, str | None]:
        return False, "auth failed"

    monkeypatch.setattr(
        "aap_migration.api.services.connection_service.ConnectionService.test_connection",
        fake_test_connection,
    )

    tested = client.post(f"/api/connections/{conn_id}/test")
    assert tested.status_code == 200
    assert tested.json() == {"ok": False, "error": "auth failed"}

    state = dependencies.get_app_state()
    session = state.db_session_factory()
    try:
        conn = session.get(
            __import__("aap_migration.api.models", fromlist=["Connection"]).Connection, conn_id
        )
        assert conn is not None
        assert conn.ping_status == "error"
        assert conn.auth_status == "error"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_job_service_start_job_persists_and_loads_from_db(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)

    async def coro(job, log):
        log("started")
        return {"answer": 42}

    job_id = service.start_job("Example Job", "demo", coro)
    job = service.get_job(job_id)
    assert job is not None and job._task is not None

    await job._task

    loaded = JobService(db_session_factory=session_factory).get_job(job_id)
    assert loaded is not None
    assert loaded.status == "completed"
    assert loaded.result == {"answer": 42}
    assert loaded.log_lines == ["started"]
    assert loaded.seq_id == 1


@pytest.mark.asyncio
async def test_job_service_resume_waiting_job(session_factory) -> None:
    service = JobService(db_session_factory=session_factory)
    job = Job("job-2", "Waiting Job", "demo")
    job.status = "waiting_for_input"
    service._jobs[job.id] = job

    assert service.resume_job(job.id) is True
    assert job.status == "running"
    assert job._resume_event.is_set() is True


def test_jobs_get_and_cancel_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    job = SimpleNamespace(
        id="job-1",
        status="completed",
        to_dict=lambda: {"id": "job-1", "status": "completed"},
    )

    class FakeService:
        def get_job(self, job_id: str):
            return job if job_id == "job-1" else None

        def cancel_job(self, job_id: str) -> bool:
            return False

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    assert jobs.get_job("job-1") == {"id": "job-1", "status": "completed"}
    assert jobs.cancel_job("job-1") == {"status": "completed"}

    with pytest.raises(Exception) as exc:
        jobs.get_job("missing")
    assert exc.value.status_code == 404


def test_list_jobs_route_returns_service_output(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = [{"id": "job-1", "status": "completed"}]

    class FakeService:
        def list_jobs(self):
            return expected

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    assert jobs.list_jobs() == expected


def test_cancel_job_route_returns_cancelled_when_service_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(status="running")

    class FakeService:
        def get_job(self, job_id: str):
            return job

        def cancel_job(self, job_id: str) -> bool:
            return True

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    assert jobs.cancel_job("job-1") == {"status": "cancelled"}


def test_cancel_job_route_raises_for_missing_job(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeService:
        def get_job(self, job_id: str):
            return None

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    with pytest.raises(Exception) as exc:
        jobs.cancel_job("missing")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_resume_job_returns_running_when_service_handles_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paused_job = SimpleNamespace(id="job-1", status="waiting_for_input", result={"x": 1})

    class FakeService:
        def get_job(self, job_id: str):
            return paused_job

        def resume_job(self, job_id: str) -> bool:
            return True

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    assert await jobs.resume_job("job-1") == {"status": "running"}


@pytest.mark.asyncio
async def test_resume_job_reexecutes_phase_after_restart(
    monkeypatch: pytest.MonkeyPatch, session_factory
) -> None:
    session = session_factory()
    session.add(
        JobRecord(
            id="job-1",
            seq_id=1,
            name="Resume Me",
            type="demo",
            status="waiting_for_input",
            result_json=None,
            output_json=None,
        )
    )
    session.commit()
    session.close()

    paused_job = SimpleNamespace(
        id="job-1",
        status="waiting_for_input",
        result={"_paused_plan_id": "plan-1", "_paused_phase_id": "phase-1"},
    )

    class FakeService:
        def get_job(self, job_id: str):
            return paused_job if job_id == "job-1" else None

        def resume_job(self, job_id: str) -> bool:
            return False

    async def fake_execute_phase(plan_id: str, phase_id: str, db) -> SimpleNamespace:
        assert plan_id == "plan-1"
        assert phase_id == "phase-1"
        return SimpleNamespace(job_id="new-job-99")

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())
    monkeypatch.setattr(
        "aap_migration.api.dependencies.get_app_state",
        lambda: dependencies.AppState(session_factory, job_service=FakeService()),
    )
    monkeypatch.setattr(
        "aap_migration.api.routers.planner.execute_phase",
        fake_execute_phase,
    )

    result = await jobs.resume_job("job-1")
    assert result == {"status": "running", "new_job_id": "new-job-99"}

    check = session_factory()
    record = check.get(JobRecord, "job-1")
    assert record is not None
    assert record.status == "resumed"
    assert record.error is None
    check.close()


@pytest.mark.asyncio
async def test_resume_job_requires_phase_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    paused_job = SimpleNamespace(id="job-1", status="waiting_for_input", result={})

    class FakeService:
        def get_job(self, job_id: str):
            return paused_job

        def resume_job(self, job_id: str) -> bool:
            return False

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    with pytest.raises(Exception) as exc:
        await jobs.resume_job("job-1")
    assert exc.value.status_code == 400
    assert "missing plan/phase reference" in exc.value.detail


def test_get_job_credentials_returns_credential_review(monkeypatch: pytest.MonkeyPatch) -> None:
    creds = [
        {
            "name": "Vault",
            "credential_type": "HashiCorp Vault",
            "organization": "Default",
            "used_by": [{"resource_type": "job_template", "resource_name": "Deploy"}],
        }
    ]

    class FakeService:
        def get_job(self, job_id: str):
            return SimpleNamespace(result={"credential_review": creds})

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    assert jobs.get_job_credentials("job-1") == creds


def test_get_job_credentials_returns_empty_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeService:
        def get_job(self, job_id: str):
            return SimpleNamespace(result={})

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    assert jobs.get_job_credentials("job-1") == []


def test_get_job_credentials_csv_streams_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeService:
        def get_job(self, job_id: str):
            return SimpleNamespace(
                result={
                    "credential_review": [
                        {
                            "name": "Vault",
                            "credential_type": "HashiCorp Vault",
                            "organization": "Default",
                            "used_by": [
                                {"resource_type": "job_template", "resource_name": "Deploy"},
                                {"resource_type": "project", "resource_name": "Website"},
                            ],
                        }
                    ]
                }
            )

    async def collect(response) -> bytes:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk.encode() if isinstance(chunk, str) else chunk
        return body

    monkeypatch.setattr(jobs, "get_job_service", lambda: FakeService())

    response = jobs.get_job_credentials_csv("job-123")
    body = asyncio.run(collect(response)).decode()

    assert response.media_type == "text/csv"
    assert response.headers["Content-Disposition"] == "attachment; filename=credentials-job-123.csv"
    assert "Credential Name,Credential Type,Organization,Used By Type,Used By Name" in body
    assert "Vault,HashiCorp Vault,Default,job_template,Deploy" in body
    assert "Vault,HashiCorp Vault,Default,project,Website" in body
