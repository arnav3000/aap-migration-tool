from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aap_migration.api.routers import iam
from aap_migration.api.schemas import IAMAnalyseRequest, IAMBenchmarkRequest, IAMReportRequest


class FakeJob:
    def __init__(self, status="completed", result=None) -> None:
        self.status = status
        self.result = result
        self._html_report = None

    def to_dict(self) -> dict:
        return {"status": self.status, "result": self.result}


class FakeJobService:
    def __init__(self) -> None:
        self.jobs: dict[str, FakeJob] = {}
        self.started = []

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def start_job(self, name, job_type, callback):
        self.started.append((name, job_type, callback))
        return f"{job_type}-job"


@pytest.mark.asyncio
async def test_iam_analyse_starts_job(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    monkeypatch.setattr(iam, "get_job_service", lambda: svc)
    conn = SimpleNamespace(
        id="conn-1",
        name="Source",
        token="encrypted",
        verify_ssl=True,
    )
    monkeypatch.setattr(
        iam.ConnectionService,
        "get",
        lambda db, connection_id: conn if connection_id == "conn-1" else None,
    )
    monkeypatch.setattr(
        iam.ConnectionService,
        "build_instance_config",
        lambda c: SimpleNamespace(url="https://source.example.com", token="secret"),
    )
    monkeypatch.setattr(iam, "_connection_token_and_ssl", lambda c, v: ("secret", True))

    class FakeStats:
        resources_scanned = 10
        permissions_found = 25
        permissions_deduplicated = 3
        team_memberships_found = 4
        system_roles_found = 2
        cross_org_shares = 1

    class FakeResult:
        stats = FakeStats()

    class FakeAnalyser:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def audit(self):
            return FakeResult()

    fake_analyser_module = type(
        "module",
        (),
        {"IAMAnalyser": FakeAnalyser},
    )
    fake_report_module = type(
        "module",
        (),
        {
            "write_iam_report": lambda result, output_dir, jf, hf: ("/tmp/a.json", "/tmp/a.html"),
            "generate_iam_html_report": lambda result: "<html>iam</html>",
        },
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "aap_migration.iam.analyser",
        fake_analyser_module,
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "aap_migration.iam.report",
        fake_report_module,
    )

    response = await iam.run_iam_analyse(
        IAMAnalyseRequest(connection_id="conn-1", output_dir="/tmp/iam"),
        db=None,
    )
    assert response.job_id == "iam-analyse-job"
    _, job_type, callback = svc.started[0]
    assert job_type == "iam-analyse"
    job = FakeJob()
    logs: list[str] = []
    result = await callback(job, logs.append)
    assert result["json_path"] == "/tmp/a.json"
    assert result["stats"]["permissions_found"] == 25
    assert job._html_report == "<html>iam</html>"
    assert any("IAM audit complete" in line for line in logs)


@pytest.mark.asyncio
async def test_iam_benchmark_captures_output(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    monkeypatch.setattr(iam, "get_job_service", lambda: svc)
    conn = SimpleNamespace(id="conn-1", name="Source", token="encrypted", verify_ssl=True)
    monkeypatch.setattr(iam.ConnectionService, "get", lambda db, cid: conn)
    monkeypatch.setattr(
        iam.ConnectionService,
        "build_instance_config",
        lambda c: SimpleNamespace(url="https://source.example.com"),
    )
    monkeypatch.setattr(iam, "_connection_token_and_ssl", lambda c, v: ("secret", True))

    def fake_benchmark(**kwargs):
        print("benchmark line 1")
        print("benchmark line 2")

    fake_benchmark_module = type("module", (), {"run_benchmark": fake_benchmark})
    monkeypatch.setitem(
        __import__("sys").modules,
        "aap_migration.iam.benchmark",
        fake_benchmark_module,
    )

    response = await iam.run_iam_benchmark(
        IAMBenchmarkRequest(connection_id="conn-1", workers=[1, 5]),
        db=None,
    )
    assert response.job_id == "iam-benchmark-job"
    _, _, callback = svc.started[0]
    job = FakeJob()
    logs: list[str] = []
    result = await callback(job, logs.append)
    assert "benchmark line 1" in result["benchmark_output"]
    assert any("benchmark line 2" in line for line in logs)


@pytest.mark.asyncio
async def test_iam_report_from_job_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    svc = FakeJobService()
    json_file = tmp_path / "audit.json"
    json_file.write_text("{}")
    svc.jobs["prior-job"] = FakeJob(result={"json_path": str(json_file)})
    monkeypatch.setattr(iam, "get_job_service", lambda: svc)

    class FakeResult:
        pass

    fake_report_module = type(
        "module",
        (),
        {
            "load_audit_result_from_json": lambda path: FakeResult(),
            "generate_iam_html_report": lambda result: "<html>regenerated</html>",
        },
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "aap_migration.iam.report",
        fake_report_module,
    )

    response = await iam.run_iam_report(IAMReportRequest(job_id="prior-job"))
    assert response.job_id == "iam-report-job"
    _, _, callback = svc.started[0]
    job = FakeJob()
    result = await callback(job, lambda _m: None)
    assert result["json_path"] == str(json_file)
    assert job._html_report == "<html>regenerated</html>"


@pytest.mark.asyncio
async def test_iam_analyse_connection_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iam.ConnectionService, "get", lambda db, cid: None)
    with pytest.raises(HTTPException, match="Connection not found"):
        await iam.run_iam_analyse(IAMAnalyseRequest(connection_id="missing"), db=None)


def test_iam_router_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = FakeJobService()
    monkeypatch.setattr(iam, "get_job_service", lambda: svc)

    with pytest.raises(HTTPException, match="Job not found"):
        iam.get_iam_result("missing")

    completed = FakeJob(status="completed", result={"json_path": "/no/such/file.json"})
    svc.jobs["done"] = completed
    with pytest.raises(HTTPException, match="JSON report not available"):
        iam.export_iam_json("done")
