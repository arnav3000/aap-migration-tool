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
