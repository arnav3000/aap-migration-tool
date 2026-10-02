"""REST API tests: job lifecycle, families, ETL, supervision."""

import time
from typing import Any

from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient

from aap_migration.api.jobs import get_job_manager


def _plant_orphaned_running(mgr: Any, job_id: str, dead_worker: Any) -> None:
    """Plant an orphaned running record + dead worker for supervisor tests.

    Single isolated helper for the one white-box planting the public submit
    API cannot express (a dead worker thread with an orphaned running
    record). All other assertions in the restart test use public accessors;
    update this helper deliberately if JobManager internals are refactored.
    """

    mgr._jobs[job_id] = {
        "job_id": job_id,
        "job_type": "probe",
        "status": "running",
        "params": {},
        "job_dir": "",
        "result": None,
        "error": None,
        "error_id": None,
        "exit_code": None,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "cancel_requested": False,
    }
    mgr._worker = dead_worker


def _cleanup_planted(mgr: Any, job_id: str) -> None:
    """Remove a planted probe record (pairs with _plant_orphaned_running)."""

    mgr._jobs.pop(job_id, None)
    mgr._funcs.pop(job_id, None)


class TestJobs:
    def test_export_lifecycle(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        _fake_success(monkeypatch, "run_export")
        created = client.post("/api/v1/exports", json={"resource_types": ["organizations"]})
        assert created.status_code == 202
        assert "job_dir" not in created.json()
        job = _wait(client, created.json()["job_id"])
        assert job["status"] == "succeeded"
        assert "job_dir" not in job
        console = client.get(f"/api/v1/jobs/{job['job_id']}/console").json()
        # The fake worker writes an artifact but no stdout: the console
        # contract distinguishes unavailable (False) from served (True),
        # so pin the value, not just the key.
        assert console["console_available"] is False
        artifacts = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts").json()
        assert "config.yaml" not in artifacts["artifacts"]
        assert "exports/orgs.json" in artifacts["artifacts"]

    def test_artifacts_truncation_keeps_int_total(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Truncated artifact walks signal truncated:true with an int total (one shape)."""
        from pathlib import Path

        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        ref = get_job_manager().get_internal(job["job_id"])
        workdir = Path(ref["job_dir"])
        # Seed beyond the limit, including secret sidecars that must stay
        # excluded even on the truncated path.
        for i in range(5):
            (workdir / f"extra-{i}.json").write_text("{}")
        (workdir / "config.yaml").write_text("x")
        (workdir / "migration_state.db").write_text("x")
        payload = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts?limit=2").json()
        assert payload["truncated"] is True
        assert isinstance(payload["total"], int)
        assert payload["total"] == 3  # bounded walk count so far (limit+offset+1)
        assert len(payload["artifacts"]) == 2
        assert "config.yaml" not in payload["artifacts"]
        assert not any(a.endswith(".db") for a in payload["artifacts"])
        full = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts?limit=500").json()
        assert full["truncated"] is False
        assert full["total"] == len(full["artifacts"])

    def test_failed_job_reports_generic_error(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services as services_mod

        def _boom(job: Any) -> Any:
            raise RuntimeError("secret-backend-detail-should-not-leak")

        monkeypatch.setattr(services_mod, "run_export", _boom)
        created = client.post("/api/v1/exports", json={}).json()
        job = _wait(client, created["job_id"])
        assert job["status"] == "failed"
        assert "secret-backend-detail" not in (job["error"] or "")
        assert "Traceback" not in (job["error"] or "")
        assert "error_id" in (job["error"] or "")

    def test_chaining_runs_chained_phase(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        _fake_success(monkeypatch, "run_export", artifact="exports/orgs.json")

        import aap_migration.api.services as services_mod

        def _fake_transform(job: Any) -> Any:
            from pathlib import Path

            workdir = Path(job["job_dir"]).resolve()
            target = workdir / "xformed" / "orgs.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}")
            return {"message": "transform ok"}

        monkeypatch.setattr(services_mod, "run_transform", _fake_transform)
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert first["status"] == "succeeded"
        second = client.post("/api/v1/transforms", json={"job_id": first["job_id"]})
        assert second.status_code == 202
        done = _wait(client, second.json()["job_id"])
        assert done["status"] == "succeeded"
        arts = client.get(f"/api/v1/jobs/{done['job_id']}/artifacts").json()
        assert any(a.startswith("xformed/") for a in arts["artifacts"])

    def test_delete_terminal_job(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        _fake_success(monkeypatch, "run_export")
        created = client.post("/api/v1/exports", json={}).json()
        job = _wait(client, created["job_id"])
        assert client.delete(f"/api/v1/jobs/{job['job_id']}").status_code == 200
        assert client.get(f"/api/v1/jobs/{job['job_id']}").status_code == 404

    def test_delete_parent_with_queued_child_conflict(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Deleting a parent referenced by a queued chained child returns 409."""
        import threading

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        _fake_success(monkeypatch, "run_export")
        _fake_success(monkeypatch, "run_transform")
        parent = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert parent["status"] == "succeeded", parent.get("error")
        # Occupy the FIFO worker so the chained child stays queued.
        manager.submit("blocker", {}, _blocker)
        assert entered.wait(timeout=30)
        try:
            child = client.post("/api/v1/transforms", json={"job_id": parent["job_id"]})
            assert child.status_code == 202, child.text
            # Parent is terminal but a queued child still needs its record.
            assert client.delete(f"/api/v1/jobs/{parent['job_id']}").status_code == 409
        finally:
            release.set()
        done_child = _wait(client, child.json()["job_id"])
        assert done_child["status"] == "succeeded", done_child.get("error")
        assert client.delete(f"/api/v1/jobs/{parent['job_id']}").status_code == 200

    def test_delete_running_job_conflict(self, client: TestClient) -> None:
        import threading

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _slow(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "slow"}

        rec = manager.submit("slow-test", {}, _slow)
        try:
            assert entered.wait(timeout=30)
            assert client.delete(f"/api/v1/jobs/{rec['job_id']}").status_code == 409
        finally:
            release.set()
        done = _wait(client, rec["job_id"])
        assert done["status"] == "succeeded"

    def test_cancel_queued_job(self, client: TestClient) -> None:
        import threading

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        # Occupy the single FIFO worker so the next submit stays queued.
        blocker = manager.submit("blocker", {}, _blocker)
        assert entered.wait(timeout=30)
        rec = manager.submit("slow-test", {}, _blocker)
        try:
            resp = client.post(f"/api/v1/jobs/{rec['job_id']}/cancel")
            assert resp.status_code == 200
            assert resp.json()["status"] == "cancelled"
            # 200 on a queued job means settled-cancelled, not pending.
            assert resp.json()["cancel_pending"] is False
        finally:
            release.set()
        done = _wait(client, blocker["job_id"])
        assert done["status"] == "succeeded"
        queued = client.get(f"/api/v1/jobs/{rec['job_id']}").json()
        assert queued["status"] == "cancelled"

    def test_cancel_running_job(self, client: TestClient) -> None:
        import threading

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _slow(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "slow"}

        rec = manager.submit("slow-test", {}, _slow)
        try:
            assert entered.wait(timeout=30)
            resp = client.post(f"/api/v1/jobs/{rec['job_id']}/cancel")
            assert resp.status_code == 200
            # Running cancel is cancel-pending: 200 but still running.
            assert resp.json()["status"] == "running"
            assert resp.json()["cancel_pending"] is True
        finally:
            release.set()
        done = _wait(client, rec["job_id"])
        assert done["status"] == "cancelled"

    def test_cancel_terminal_job(self, client: TestClient) -> None:
        manager = get_job_manager()
        rec = manager.submit("fast", {}, lambda job: {"message": "ok"})
        done = _wait(client, rec["job_id"])
        assert done["status"] == "succeeded"
        resp = client.post(f"/api/v1/jobs/{rec['job_id']}/cancel")
        assert resp.status_code == 409

    def test_cancel_unknown_job(self, client: TestClient) -> None:
        assert client.post("/api/v1/jobs/missing/cancel").status_code == 404

    def test_job_not_found(self, client: TestClient) -> None:
        assert client.get("/api/v1/jobs/missing").status_code == 404

    def test_negative_tail_limit_rejected(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        _fake_success(monkeypatch, "run_export")
        created = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert client.get(f"/api/v1/jobs/{created['job_id']}/console?tail=0").status_code == 422
        assert client.get("/api/v1/jobs?limit=-1").status_code == 422

    def test_status_filter_validated(self, client: TestClient) -> None:
        assert client.get("/api/v1/jobs?status=bogus").status_code == 422

    def test_iam_report_pending_guard(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading

        import aap_migration.api.services as services_mod

        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "export ok"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        pending = client.post("/api/v1/exports", json={}).json()
        assert entered.wait(timeout=30)
        try:
            # Fail fast at submit for non-succeeded chaining (no silent partial).
            resp = client.post("/api/v1/iam/report", json={"job_id": pending["job_id"]})
            assert resp.status_code == 409
            assert "only succeeded jobs" in resp.json()["detail"]
        finally:
            release.set()
        done = _wait(client, pending["job_id"])
        assert done["status"] == "succeeded"

    def test_iam_report_worker_pending_no_crash(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Direct worker guard: pending referenced result never raises AttributeError."""
        import aap_migration.api.services as services_mod
        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        pending = client.post("/api/v1/exports", json={}).json()
        ref = get_job_manager().get_internal(pending["job_id"])
        assert ref["status"] in ("queued", "running", "succeeded")
        # Simulate a queued reference with no result yet.
        fake_job: Any = {"params": {"job_id": pending["job_id"]}, "job_dir": ref["job_dir"]}
        try:
            services_mod.run_iam_report(fake_job)
        except ValueError as exc:
            # No json result yet on the pending reference: the worker names
            # the missing report (not the chaining rule, which is submit-time).
            assert "required" in str(exc)
        except AttributeError as err:
            raise AssertionError("run_iam_report raised AttributeError on pending job") from err


class TestFamilyCoverage:
    """Mocked-worker round-trips for router families without coverage.

    Each test asserts 202 acceptance then a succeeded terminal state plus
    artifact keys, so wiring errors (wrong service target, missing need
    validation, unserializable results) fail loudly instead of shipping.
    """

    def _roundtrip(
        self,
        client: TestClient,
        monkeypatch: Any,
        method: str,
        path: str,
        worker: str,
        json: Any = None,
    ) -> Any:
        _fake_success(monkeypatch, worker)
        resp = client.post(path, json={} if json is None else json)
        assert resp.status_code == 202, resp.text
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert "job_dir" not in job
        # Worker identity proof: the fake reports its own patched name, so
        # a copy-paste wiring error to a neighboring worker fails here.
        assert job["result"]["message"] == f"{worker} ok"
        assert "artifacts" in job["result"]
        return job

    def test_prep_cleanup(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(client, monkeypatch, "post", "/api/v1/prep", "run_prep")
        self._roundtrip(client, monkeypatch, "post", "/api/v1/cleanup", "run_cleanup")

    def test_validations(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(client, monkeypatch, "post", "/api/v1/validations", "run_validate")

    def test_retry_failed(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(client, monkeypatch, "post", "/api/v1/retry/failed", "run_retry_failed")

    def test_credentials_family(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(
            client, monkeypatch, "post", "/api/v1/credentials/compare", "run_credential_compare"
        )
        self._roundtrip(
            client, monkeypatch, "post", "/api/v1/credentials/migrate", "run_credential_migrate"
        )
        self._roundtrip(
            client, monkeypatch, "post", "/api/v1/credentials/report", "run_credential_compare"
        )

    def test_iam_family(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(client, monkeypatch, "post", "/api/v1/iam/audit", "run_iam_audit")
        self._roundtrip(client, monkeypatch, "post", "/api/v1/iam/migrate", "run_iam_migrate")
        self._roundtrip(client, monkeypatch, "post", "/api/v1/iam/benchmark", "run_iam_benchmark")
        assert client.get("/api/v1/iam/checkpoint").status_code == 200

    def test_analysis_reporting(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(
            client,
            monkeypatch,
            "post",
            "/api/v1/analysis/dependencies",
            "run_analyze_dependencies",
            json={"analyze_all": True},
        )
        self._roundtrip(
            client, monkeypatch, "post", "/api/v1/reports/migration", "run_migration_report"
        )
        self._roundtrip(
            client, monkeypatch, "post", "/api/v1/reports/enhanced", "run_enhanced_report"
        )
        self._roundtrip(
            client,
            monkeypatch,
            "post",
            "/api/v1/reports/project-failures",
            "run_project_failures",
        )

    def test_state_export_roundtrip(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        self._roundtrip(client, monkeypatch, "post", "/api/v1/state/export", "run_state_export")

    def test_connection_test_missing_is_404(self, client: TestClient) -> None:
        assert client.post("/api/v1/connections/missing/test").status_code == 404


class TestWiringSpies11:
    """CLI-boundary spies for the 11 roundtrip-only workers.

    _roundtrip proves acceptance + envelope; these spies prove param
    mapping at the CLI boundary (wrong worker or dropped param fails).
    """

    def _stub_ctx(self, monkeypatch: Any, tmp_path: Any, params: dict) -> Any:
        import contextlib
        import types

        import aap_migration.api.services._core as core

        workdir = tmp_path / "job"
        workdir.mkdir(exist_ok=True)
        (workdir / "reports").mkdir(exist_ok=True)
        (workdir / "schemas").mkdir(exist_ok=True)
        (workdir / "exports").mkdir(exist_ok=True)
        config = types.SimpleNamespace(
            dry_run=False,
            export=types.SimpleNamespace(records_per_file=1000),
            performance=types.SimpleNamespace(
                project_patch_batch_size=50, project_patch_batch_interval=0
            ),
        )

        @contextlib.contextmanager
        def _fake_ctx(job: Any, **kwargs: Any) -> Any:
            yield (types.SimpleNamespace(config=config), config, workdir, params)

        monkeypatch.setattr(core, "chained_ctx", _fake_ctx)
        for _mod_name in (
            "aap_migration.api.services.etl",
            "aap_migration.api.services.reporting",
            "aap_migration.api.services.maintenance",
            "aap_migration.api.services.credentials",
            "aap_migration.api.services.iam",
        ):
            try:
                import importlib as _il

                _mod = _il.import_module(_mod_name)
                if hasattr(_mod, "chained_ctx"):
                    monkeypatch.setattr(_mod, "chained_ctx", _fake_ctx)
            except Exception:
                pass
        return workdir

    def test_transform_maps_resource_type(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        params = {"resource_types": ["hosts"], "quiet": True}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}
        monkeypatch.setattr(etl, "call_command", lambda cmd, ctx, **kw: seen.update(cmd=cmd, **kw))
        monkeypatch.setattr(etl, "is_noop_scope", lambda p: False)
        etl.run_transform({"job_id": "t", "job_dir": str(tmp_path / "job"), "params": params})
        assert seen["cmd"] == "transform"
        assert seen["resource_type"] == ("hosts",)

    def test_patch_projects_maps_batch(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        params = {"batch_size": 25}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}
        monkeypatch.setattr(etl, "call_command", lambda cmd, ctx, **kw: seen.update(cmd=cmd, **kw))
        etl.run_patch_projects({"job_id": "p", "job_dir": str(tmp_path / "job"), "params": params})
        assert seen["cmd"] == "patch-projects"
        assert seen["batch_size"] == 25

    def test_migrate_resume_maps_phase(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        params = {"from_phase": "hosts"}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}
        monkeypatch.setattr(etl, "call_command", lambda cmd, ctx, **kw: seen.update(cmd=cmd, **kw))
        etl.run_migrate_resume({"job_id": "r", "job_dir": str(tmp_path / "job"), "params": params})
        assert seen["cmd"] == "resume"
        assert seen["from_phase"] == "hosts"

    def test_credential_compare_calls_coordinator(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.credentials as cred

        params: dict = {}
        self._stub_ctx(monkeypatch, tmp_path, params)
        called: dict = {}

        class _C:
            async def compare_and_verify_credentials(self, report_path: str = "") -> Any:
                called["report"] = report_path
                return {"missing_count": 1}

        monkeypatch.setattr(cred, "_credential_coordinator", lambda ctx: _C())
        out = cred.run_credential_compare(
            {"job_id": "c", "job_dir": str(tmp_path / "job"), "params": params}
        )
        assert called["report"].endswith("credential-comparison.md")
        assert out["report"] == "reports/credential-comparison.md"

    def test_credential_migrate_maps_branches(self, tmp_path: Any, monkeypatch: Any) -> None:
        # Covered directly by TestCredentialMigrateBranches (no-action vs
        # migrate-all); this spy pins the migrate_all phase allowlist.
        import aap_migration.api.services.credentials as cred

        params: dict = {}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}

        class _C:
            async def compare_and_verify_credentials(self, report_path: str = "") -> Any:
                return {"missing_count": 2}

            async def migrate_all(self, **kw: Any) -> Any:
                seen.update(kw)
                return {"migrated": 1}

        monkeypatch.setattr(cred, "_credential_coordinator", lambda ctx: _C())
        cred.run_credential_migrate(
            {"job_id": "c", "job_dir": str(tmp_path / "job"), "params": params}
        )
        assert seen["only_phases"] == ["organizations", "credentials"]

    def test_iam_audit_maps_source(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.iam as iam

        params = {"skip_ssl_verify": False}
        self._stub_ctx(monkeypatch, tmp_path, params)
        monkeypatch.setattr(
            iam, "_iam_connections", lambda pdict, need="source": ({"url": "u", "token": "t"}, None)
        )
        seen: dict = {}
        monkeypatch.setattr(
            iam, "_run_iam_audit", lambda pd, wd, s: seen.update(pdict=pd) or {"message": "ok"}
        )
        iam.run_iam_audit({"job_id": "a", "job_dir": str(tmp_path / "job"), "params": params})
        assert seen["pdict"] is not None

    def test_iam_migrate_rejects_exclusive_flags(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.iam as iam

        params = {"skip_user_roles": True, "users_only": True}
        self._stub_ctx(monkeypatch, tmp_path, params)
        monkeypatch.setattr(
            iam, "_iam_connections", lambda pdict, need="both": ({"url": "u"}, {"url": "v"})
        )
        try:
            iam.run_iam_migrate({"job_id": "m", "job_dir": str(tmp_path / "job"), "params": params})
            raise AssertionError("expected exclusive-flags ValueError")
        except ValueError as exc:
            assert "mutually exclusive" in str(exc)

    def test_analyze_dependencies_requires_scope(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.reporting as rep

        params: dict = {}
        self._stub_ctx(monkeypatch, tmp_path, params)
        try:
            rep.run_analyze_dependencies(
                {"job_id": "d", "job_dir": str(tmp_path / "job"), "params": params}
            )
            raise AssertionError("expected scope ValueError")
        except ValueError as exc:
            assert "analyze_all" in str(exc)

    def test_migration_report_maps_format(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.reporting as rep

        params = {"output_format": "markdown", "resource_type": "hosts"}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}
        monkeypatch.setattr(rep, "call_command", lambda cmd, ctx, **kw: seen.update(cmd=cmd, **kw))
        rep.run_migration_report(
            {"job_id": "m", "job_dir": str(tmp_path / "job"), "params": params}
        )
        assert seen["cmd"] == "migration-report"
        assert seen["resource_type"] == "hosts"

    def test_enhanced_report_maps_org(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.reporting as rep

        params = {"output_format": "csv", "organization": "Default"}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}
        monkeypatch.setattr(rep, "call_command", lambda cmd, ctx, **kw: seen.update(cmd=cmd, **kw))
        rep.run_enhanced_report({"job_id": "e", "job_dir": str(tmp_path / "job"), "params": params})
        assert seen["cmd"] == "enhanced-report"
        assert seen["organization"] == "Default"

    def test_project_failures_runs_command(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.reporting as rep

        params: dict = {}
        self._stub_ctx(monkeypatch, tmp_path, params)
        seen: dict = {}
        monkeypatch.setattr(rep, "call_command", lambda cmd, ctx, **kw: seen.update(cmd=cmd, **kw))
        out = rep.run_project_failures(
            {"job_id": "p", "job_dir": str(tmp_path / "job"), "params": params}
        )
        assert seen["cmd"] == "analyze-project-failures"
        assert out["report"].endswith("PROJECT-FAILURES-REPORT.md")

    def test_prep_pings_both_sides(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.maintenance as m

        params: dict = {}
        self._stub_ctx(monkeypatch, tmp_path, params)
        pinged: list = []

        class _C:
            async def get(self, endpoint: str, **kw: Any) -> Any:
                pinged.append(endpoint)
                return {}

            async def get_version(self) -> Any:
                return "2.5.0"

        import types

        ctx = types.SimpleNamespace(source_client=_C(), target_client=_C())
        import contextlib

        import aap_migration.api.services._core as core

        @contextlib.contextmanager
        def _fake_prep_ctx(job: Any, **kw: Any) -> Any:
            cfg = types.SimpleNamespace(
                state=types.SimpleNamespace(db_path=""),
                paths=types.SimpleNamespace(schema_dir=str(tmp_path)),
                ignored_endpoints={"common": [], "source": [], "target": []},
            )
            yield (ctx, cfg, tmp_path / "job", params)

        monkeypatch.setattr(core, "chained_ctx", _fake_prep_ctx)
        monkeypatch.setattr(m, "chained_ctx", _fake_prep_ctx)

        async def _fake_discover(*a: Any, **k: Any) -> Any:
            return {"endpoints": {}}

        monkeypatch.setattr("aap_migration.prep.discover_endpoints", _fake_discover)

        async def _fake_gen(*a: Any, **k: Any) -> Any:
            return {}

        monkeypatch.setattr("aap_migration.prep.generate_schema", _fake_gen)
        monkeypatch.setattr("aap_migration.prep.compare_schemas", lambda *a, **k: {})
        monkeypatch.setattr("aap_migration.prep.save_endpoints", lambda *a, **k: None)
        monkeypatch.setattr("aap_migration.prep.save_schema", lambda *a, **k: None)
        monkeypatch.setattr("aap_migration.prep.save_comparison", lambda *a, **k: None)
        monkeypatch.setattr(
            "aap_migration.utils.version_validation.validate_version_compatibility",
            lambda *a, **k: None,
        )
        out = m.run_prep({"job_id": "p", "job_dir": str(tmp_path / "job"), "params": params})
        assert pinged.count("ping/") >= 2
        assert out["message"] == "Prep complete"


class TestCoreEtl:
    """Execution tests for core ETL/sync endpoints (#4).

    Every long-running CLI capability must prove submit->succeeded wiring
    with per-endpoint round-trips, and the sync gates must prove their
    404/409 branches, so a wrong service target or broken submit_chained
    call fails loudly.
    """

    def _roundtrip(
        self, client: TestClient, monkeypatch: Any, path: str, worker: str, json: Any = None
    ) -> Any:
        _fake_success(monkeypatch, worker)
        resp = client.post(path, json={} if json is None else json)
        assert resp.status_code == 202, resp.text
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["message"] == f"{worker} ok"
        assert "artifacts" in job["result"]
        return job

    def test_migrations_imports_patch_granular(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        self._roundtrip(client, monkeypatch, "/api/v1/migrations", "run_migrate")
        self._roundtrip(client, monkeypatch, "/api/v1/imports", "run_import")
        self._roundtrip(client, monkeypatch, "/api/v1/imports/patch-projects", "run_patch_projects")
        self._roundtrip(
            client,
            monkeypatch,
            "/api/v1/imports/granular",
            "run_granular_import",
            json={"steps": ["organizations"]},
        )

    def test_resume_onto_failed(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        import aap_migration.api.services as services_mod

        def _boom(job: Any) -> Any:
            raise RuntimeError("phase failure")

        monkeypatch.setattr(services_mod, "run_export", _boom)
        failed = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert failed["status"] == "failed"
        _fake_success(monkeypatch, "run_migrate_resume")
        resp = client.post("/api/v1/migrations/resume", json={"job_id": failed["job_id"]})
        assert resp.status_code == 202, resp.text
        resumed = _wait(client, resp.json()["job_id"])
        assert resumed["status"] == "succeeded", resumed.get("error")

    def test_migration_status_with_and_without_job(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        body = client.get("/api/v1/migrations/status").json()
        assert "resource_stats" in body
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        # Real workers record progress in the job dir; the fake worker does
        # not, so materialize the job DB the way a real export would.
        ref = get_job_manager().get_internal(first["job_id"])
        open_state(str(Path(ref["job_dir"]) / "migration_state.db"))
        scoped = client.get(f"/api/v1/migrations/status?job_id={first['job_id']}").json()
        assert "resource_stats" in scoped
        assert client.get("/api/v1/migrations/status?job_id=missing").status_code == 404

    def test_dependency_gate_branches(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading

        import aap_migration.api.services as services_mod

        assert (
            client.post("/api/v1/validations/dependencies", json={"job_id": "missing"}).status_code
            == 404
        )
        assert (
            client.post(
                "/api/v1/imports/check-dependencies", json={"job_id": "missing"}
            ).status_code
            == 404
        )
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "export ok"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        running = client.post("/api/v1/exports", json={}).json()
        assert entered.wait(timeout=30)
        try:
            assert (
                client.post(
                    "/api/v1/validations/dependencies", json={"job_id": running["job_id"]}
                ).status_code
                == 409
            )
            assert (
                client.post(
                    "/api/v1/imports/check-dependencies",
                    json={"job_id": running["job_id"]},
                ).status_code
                == 409
            )
        finally:
            release.set()
        assert _wait(client, running["job_id"])["status"] == "succeeded"

    def test_prep_schemas_and_ready(self, client: TestClient) -> None:
        assert client.get("/api/v1/prep/schemas").status_code == 200
        body = client.get("/api/v1/ready").json()
        assert body["ready"] is True


class TestSupervision:
    """Backpressure, timeout, restart, and eviction paths (#6)."""

    def test_queue_full_returns_429(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        import threading

        import aap_migration.api.jobs as jobs_mod
        import aap_migration.api.services as services_mod

        monkeypatch.setattr(jobs_mod._config, "MAX_QUEUE_DEPTH", 1)
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "export ok"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        first = client.post("/api/v1/exports", json={})
        assert first.status_code == 202
        assert entered.wait(timeout=30)
        try:
            resp = client.post("/api/v1/exports", json={})
            assert resp.status_code == 429
        finally:
            release.set()
        assert _wait(client, first.json()["job_id"])["status"] == "succeeded"

    def test_job_timeout_fails_loudly(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services as services_mod

        manager = get_job_manager()
        manager.job_timeout = 0.2

        def _slow(job: Any) -> Any:
            time.sleep(2)
            return {"message": "too late"}

        monkeypatch.setattr(services_mod, "run_export", _slow)
        created = client.post("/api/v1/exports", json={}).json()
        job = _wait(client, created["job_id"])
        assert job["status"] == "failed"
        assert "timed out" in (job["error"] or "")
        assert "Traceback" not in (job["error"] or "")
        assert "error_id" in (job["error"] or "")
        # The timed-out attempt fences its workdir until the orphan drains:
        # pin it here so a leaked fence cannot silently spill into the next
        # test as a surprising 429.
        fenced = manager.pressure()["fenced_dirs"]
        assert isinstance(fenced, int) and fenced >= 1

    def test_dead_worker_restart_reaps_running(self, client: TestClient, monkeypatch: Any) -> None:
        import threading

        mgr = get_job_manager()
        dead = threading.Thread(target=lambda: None, name="dead-probe")
        dead.start()
        dead.join()
        assert not dead.is_alive()
        # Planting isolated in _plant_orphaned_running: the supervisor path
        # (dead worker + orphaned running record) is unreachable via the
        # public submit API, which always starts a live worker. All asserts
        # below use public accessors.
        planted = "planted-running"
        _plant_orphaned_running(mgr, planted, dead)
        try:
            mgr.ensure_worker()
            assert mgr.worker_alive()
            assert mgr.get(planted)["status"] == "failed"
            assert "error_id" in (mgr.get(planted)["error"] or "")
            assert planted in {j["job_id"] for j in mgr.list_jobs()}
            assert mgr.get_internal(planted)["job_dir"] == ""
        finally:
            _cleanup_planted(mgr, planted)

    def test_oldest_terminal_evicted(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        import aap_migration.api.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod._config, "MAX_JOBS", 3)
        _fake_success(monkeypatch, "run_export")
        ids = [
            _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])["job_id"]
            for _ in range(4)
        ]
        assert client.get(f"/api/v1/jobs/{ids[0]}").status_code == 404
        mgr = get_job_manager()
        # Eviction proof via public accessors: the oldest id is unknown
        # while the newest three remain listed (no _funcs registry read).
        import pytest

        with pytest.raises(KeyError):
            mgr.get(ids[0])
        assert len(mgr.list_jobs()) == 3
        assert {j["job_id"] for j in mgr.list_jobs()} == set(ids[1:])
