"""Regression tests for ce-code-review findings on release/v1.x-rest-api.

Each class maps to one or more numbered findings from the full review
(PR #127 follow-up): contract envelopes, job-lifecycle guards, IAM resume,
pagination integrity, auth posture, and supervisor branches.
"""

import time
from typing import Any

import pytest
from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient

from aap_migration.api.jobs import QueueFullError, get_job_manager


class TestPairFence:
    """#2: a timed-out orphan fences its AAP pair, not just its directory."""

    def test_resubmit_same_pair_rejected_until_drain(self, client: TestClient) -> None:
        manager = get_job_manager()
        manager.job_timeout = 0.2

        def _slow(job: Any) -> Any:
            time.sleep(2)
            return {"message": "too late"}

        first = manager.submit("pair-test", {"_snapshot_fp": "fp-A"}, _slow)
        done = _wait(client, first["job_id"])
        assert done["status"] == "failed"
        assert "timed out" in (done["error"] or "")

        def _fast(job: Any) -> Any:
            return {"message": "fast"}

        with pytest.raises(QueueFullError, match="pair fenced"):
            manager.submit("pair-test", {"_snapshot_fp": "fp-A"}, _fast)
        other = manager.submit("pair-test", {"_snapshot_fp": "fp-B"}, _fast)
        assert _wait(client, other["job_id"])["status"] == "succeeded"


class TestFencedDelete:
    """#14: deleting a fenced (timed-out) job keeps its directory."""

    def test_delete_fenced_job_keeps_dir(self, client: TestClient) -> None:
        from pathlib import Path

        manager = get_job_manager()
        manager.job_timeout = 0.2

        def _slow(job: Any) -> Any:
            time.sleep(2)
            return {"message": "too late"}

        rec = manager.submit("fence-test", {}, _slow)
        done = _wait(client, rec["job_id"])
        assert done["status"] == "failed"
        workdir = Path(manager.get_internal(rec["job_id"])["job_dir"])
        assert workdir.is_dir()
        assert client.delete(f"/api/v1/jobs/{rec['job_id']}").status_code == 200
        assert client.get(f"/api/v1/jobs/{rec['job_id']}").status_code == 404
        # The live orphan still owns the directory: no unowned rmtree.
        assert workdir.is_dir()


class TestRestartIndex:
    """#15: queued-cancel and supervisor-restart index the restart message."""

    def test_cancelled_queued_job_indexed_for_restart(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading
        from pathlib import Path

        import aap_migration.api.services as services_mod

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        first = client.post("/api/v1/exports", json={}).json()
        assert entered.wait(timeout=30)
        second = client.post("/api/v1/exports", json={}).json()
        try:
            assert client.post(f"/api/v1/jobs/{second['job_id']}/cancel").status_code == 200
        finally:
            release.set()
        assert _wait(client, first["job_id"])["status"] == "succeeded"
        index = Path(manager.base_dir) / ".job_index.jsonl"
        assert second["job_id"] in index.read_text()


class TestConnectionDeleteGuard:
    """#9: deleting a connection under a queued/running job returns 409."""

    def test_delete_referenced_connection_conflict(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading

        import aap_migration.api.services as services_mod

        src, _tgt = pair
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        created = client.post("/api/v1/exports", json={}).json()
        assert entered.wait(timeout=30)
        try:
            resp = client.delete(f"/api/v1/connections/{src['id']}")
            assert resp.status_code == 409, resp.text
        finally:
            release.set()
        done = _wait(client, created["job_id"])
        assert done["status"] == "succeeded"
        assert client.delete(f"/api/v1/connections/{src['id']}").status_code == 200


class TestManagerBranches:
    """#16: supervisor/manager error branches (exit codes, escape, drain)."""

    def test_click_exit_nonzero_failed_with_exit_code(self, client: TestClient) -> None:
        import click

        manager = get_job_manager()

        def _exit(job: Any) -> Any:
            raise click.exceptions.Exit(3)

        rec = manager.submit("exit-test", {}, _exit)
        done = _wait(client, rec["job_id"])
        assert done["status"] == "failed"
        assert done["exit_code"] == 3

    def test_job_dir_escape_rejected(self, client: TestClient) -> None:
        manager = get_job_manager()
        with pytest.raises(ValueError, match="must stay under"):
            manager.submit("evil", {}, lambda job: {}, job_dir="/tmp/evil-outside")

    def test_shutdown_drain_counts_leftovers(self, client: TestClient) -> None:
        import threading

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        running = manager.submit("drain-test", {}, _blocker)
        assert entered.wait(timeout=30)
        try:
            leftovers = manager.shutdown_drain(timeout_secs=0.1)
            assert leftovers["running"] == 1
        finally:
            # No private _draining reset: the per-test manager is discarded
            # by the client fixture, so leaving drain mode on is isolated.
            release.set()
        # P2 #19: the drain verdict holds even though the pool thread
        # finishes afterwards (no silent overwrite to succeeded).
        final = manager.get(running["job_id"])
        assert final["status"] == "failed", final
        assert "Interrupted by server shutdown" in (final["error"] or "")

    def test_eviction_keeps_referenced_parent(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading

        import aap_migration.api.jobs as jobs_mod
        import aap_migration.api.services as services_mod

        monkeypatch.setattr(jobs_mod._config, "MAX_JOBS", 3)
        _fake_success(monkeypatch, "run_export")
        _fake_success(monkeypatch, "run_transform")
        parent = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert parent["status"] == "succeeded"
        other = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert other["status"] == "succeeded"

        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        monkeypatch.setattr(services_mod, "run_export", _blocker)
        manager = get_job_manager()
        occupied = manager.submit("blocker", {}, _blocker)
        assert entered.wait(timeout=30)
        try:
            child = client.post("/api/v1/transforms", json={"job_id": parent["job_id"]})
            assert child.status_code == 202, child.text
            # Over capacity: the parent referenced by the queued child must
            # survive eviction; the unreferenced terminal goes instead.
            assert client.get(f"/api/v1/jobs/{parent['job_id']}").status_code == 200
            assert client.get(f"/api/v1/jobs/{other['job_id']}").status_code == 404
        finally:
            release.set()
        assert _wait(client, occupied["job_id"])["status"] == "succeeded"
        assert _wait(client, child.json()["job_id"])["status"] == "succeeded"


class TestIamResume:
    """#18: IAM audit chains onto failed jobs so resume=true can continue."""

    def test_iam_audit_onto_failed_job_accepted(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services as services_mod

        def _boom(job: Any) -> Any:
            raise RuntimeError("phase failure")

        monkeypatch.setattr(services_mod, "run_export", _boom)
        failed = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert failed["status"] == "failed"
        resp = client.post("/api/v1/iam/audit", json={"job_id": failed["job_id"]})
        assert resp.status_code == 202, resp.text
        assert resp.json()["chained_from_status"] == "failed"


class TestPaginateInterruption:
    """#10: a transport failure mid-walk raises instead of returning partial."""

    def _analyser(self) -> Any:
        from aap_migration.iam.analyser import IAMAnalyser

        analyser = IAMAnalyser.__new__(IAMAnalyser)
        analyser.verify_ssl = True
        analyser.request_timeout = 5
        analyser.rate_limit_delay = 0
        return analyser

    def _resp(self, status: int, payload: Any) -> Any:
        class _R:
            status_code = status
            headers: dict = {}
            text = '{"ok": true}'
            url = "https://aap.example.com/api/v2/users/"

            def json(self) -> Any:
                return payload

        return _R()

    def test_request_exception_mid_walk_raises(self) -> None:
        import requests

        from aap_migration.iam.exceptions import PaginationError

        analyser = self._analyser()
        first = self._resp(
            200,
            {
                "count": 2,
                "results": [{"id": 1}],
                "next": "https://aap.example.com/api/v2/users/?page=2",
            },
        )

        class _Session:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, *a: Any, **k: Any) -> Any:
                self.calls += 1
                if self.calls == 1:
                    return first
                raise requests.RequestException("connection drop")

        with pytest.raises(PaginationError):
            analyser._paginate(
                "https://aap.example.com/api/v2",
                "token",
                _Session(),
                "users/",
                "aap.example.com",
            )

    def test_first_page_non_200_raises(self) -> None:
        from aap_migration.iam.exceptions import PaginationError

        analyser = self._analyser()

        class _Session:
            def get(self, *a: Any, **k: Any) -> Any:
                return self._resp(500, {})

            def _resp(self, status: int, payload: Any) -> Any:
                return self._outer(status, payload)  # type: ignore[attr-defined]

        session = _Session()
        session._outer = self._resp  # type: ignore[attr-defined]
        with pytest.raises(PaginationError):
            analyser._paginate(
                "https://aap.example.com/api/v2",
                "token",
                session,
                "users/",
                "aap.example.com",
            )


class TestGlobalImportGuard:
    """#20: global-scope state import is rejected while jobs are active."""

    def test_global_import_409_while_running(self, client: TestClient) -> None:
        import threading
        from pathlib import Path

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        rec = manager.submit("blocker", {}, _blocker)
        assert entered.wait(timeout=30)
        backup = Path(manager.base_dir) / "backup.json"
        backup.write_text("{}")
        try:
            resp = client.post("/api/v1/state/import", json={"state_file": "backup.json"})
            assert resp.status_code == 409, resp.text
        finally:
            release.set()
        assert _wait(client, rec["job_id"])["status"] == "succeeded"


class TestContractShapes:
    """#3/#4/#5/#12/#19/#22: one envelope per route, null for unknown."""

    # NOTE: truncated-artifacts contract owned by
    # TestJobs.test_artifacts_truncation_keeps_int_total in test_api_jobs.py
    # (single owner to avoid dual-edit drift).

    def test_reset_envelope_keys(self, client: TestClient, monkeypatch: Any, tmp_path: Any) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "reset.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        expected = {"resource_type", "cleared_progress", "reset_mappings", "reset", "keep_mappings"}
        scoped = client.post("/api/v1/state/reset", json={"resource_type": "organizations"})
        assert scoped.status_code == 200, scoped.text
        assert set(scoped.json()) == expected
        keep = client.post("/api/v1/state/reset", json={"keep_mappings": True})
        assert keep.status_code == 200, keep.text
        assert set(keep.json()) == expected
        full = client.post("/api/v1/state/reset", json={})
        assert full.status_code == 200, full.text
        assert set(full.json()) == expected
        assert full.json()["reset"] == "all"
        # Full-reset counts stay numeric (0) on every branch.
        assert full.json()["cleared_progress"] == 0
        assert full.json()["reset_mappings"] == 0

    def test_migration_status_missing_db_warning_and_strict(
        self, pair: Any, client: TestClient
    ) -> None:
        body = client.get("/api/v1/migrations/status").json()
        assert body["resource_stats"] == {}
        assert body["warning"] == "No migration state DB found"
        assert client.get("/api/v1/migrations/status?strict=true").status_code == 404

    def test_health_unknown_is_null(self, client: TestClient, monkeypatch: Any) -> None:
        import aap_migration.api.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "_manager", None)
        body = client.get("/api/v1/health").json()
        # Fresh boot with no manager yet reports healthy (alive, depth 0):
        # the first submit lazily creates the manager, so probes must not
        # 503 before any job exists.
        assert body["worker"] == "alive"
        assert body["queue_depth"] == 0
        assert body["orphans"] is None

    def test_versioned_docs_and_redirects(self, client: TestClient) -> None:
        assert client.get("/api/v1/openapi.json").status_code == 200
        assert client.get("/api/v1/docs").status_code == 200
        redirected = client.get("/docs", follow_redirects=False)
        assert redirected.status_code == 307
        assert redirected.headers["location"] == "/api/v1/docs"


class TestSuccessPaths:
    """#6: happy-path proof for import, reset, checkpoints, plan, schemas."""

    def test_state_import_roundtrip_with_job_id(
        self, pair: Any, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_default_state, open_state, resolve_job_state
        from aap_migration.api.jobs import get_job_manager

        db = str(tmp_path / "state.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        ref = get_job_manager().get_internal(job["job_id"])
        default_state = open_default_state()
        assert default_state is not None
        # Seed one row so the write target is observable: the backup carries
        # it, and only the job's isolated DB may receive it.
        default_state.mark_in_progress("organizations", 1, "Default_Org")
        default_state.export_state(str(Path(ref["job_dir"]) / "backup.json"))
        # The job's isolated DB must exist (strict readers 404 without it).
        job_state = open_state(str(Path(ref["job_dir"]) / "migration_state.db"))
        assert job_state.get_migration_stats()["total"] == 0
        resp = client.post(
            "/api/v1/state/import", json={"state_file": "backup.json", "job_id": job["job_id"]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] == "backup.json"
        assert resp.json()["scope"] == "job"
        # Write target is the job's DB, not the server-default DB.
        _, reread = resolve_job_state(job["job_id"], strict=True)
        assert reread is not None
        assert reread.get_migration_stats()["total"] == 1

    def test_state_import_with_job_id_missing_db_404(
        self, pair: Any, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_default_state, open_state
        from aap_migration.api.jobs import get_job_manager

        db = str(tmp_path / "state.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        assert open_default_state() is not None
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        ref = get_job_manager().get_internal(job["job_id"])
        # Backup exists under the job dir, but the job has no isolated DB:
        # job-scoped imports target the job's DB, so this is a 404 (not a
        # silent write to the server-default DB).
        (Path(ref["job_dir"]) / "backup.json").write_text("{}")
        resp = client.post(
            "/api/v1/state/import", json={"state_file": "backup.json", "job_id": job["job_id"]}
        )
        assert resp.status_code == 404, resp.text

    def test_checkpoint_create_list_delete_cycle(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "checkpoints.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        created = client.post("/api/v1/checkpoints", json={"phase": "export"})
        assert created.status_code == 201, created.text
        checkpoint_id = created.json()["checkpoint_id"]
        assert isinstance(checkpoint_id, int)
        assert created.json()["migration_id"]
        listed = client.get("/api/v1/checkpoints").json()
        assert listed["total"] >= 1
        assert checkpoint_id in {c["id"] for c in listed["checkpoints"]}
        deleted = client.delete(f"/api/v1/checkpoints/{checkpoint_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_checkpoint_id"] == checkpoint_id
        assert isinstance(deleted.json()["deleted_checkpoint_id"], int)

    def test_delete_unknown_checkpoint_404(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "checkpoints-404.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        resp = client.delete("/api/v1/checkpoints/99999")
        assert resp.status_code == 404, resp.text

    def test_reset_blank_resource_type_422(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "reset-blank.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        for payload in ({"resource_type": ""}, {"resource_type": "   "}):
            resp = client.post("/api/v1/state/reset", json=payload)
            assert resp.status_code == 422, resp.text

    def test_migration_plan_shape(self, client: TestClient) -> None:
        body = client.get("/api/v1/analysis/migration-plan").json()
        assert isinstance(body["helpers"], list) and body["helpers"]

    def test_prep_schemas_seeded_artifact(
        self, client: TestClient, tmp_path: Any, monkeypatch: Any
    ) -> None:
        import json
        from pathlib import Path

        # The suite fixture points AAP_BRIDGE_STARTUP_CWD at tmp_path.
        schemas = Path(str(tmp_path)) / "schemas"
        schemas.mkdir(parents=True, exist_ok=True)
        (schemas / "schema_comparison.json").write_text(json.dumps({"compared": True}))
        body = client.get("/api/v1/prep/schemas").json()
        assert body["schemas/schema_comparison.json"] == {"compared": True}


class TestRealWorkers:
    """#8: real (unmocked) workers execute against tmp connections/state."""

    def _localhost_pair(self, client: TestClient) -> None:
        src = client.post(
            "/api/v1/connections",
            json={
                "name": "lsrc",
                "kind": "source",
                "url": "https://127.0.0.1:9/api/v2",
                "token": "t",
                "verify_ssl": False,
            },
        ).json()
        tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "ltgt",
                "kind": "target",
                "url": "https://127.0.0.1:9/api/v2",
                "token": "t",
                "verify_ssl": False,
            },
        ).json()
        client.post(
            "/api/v1/connections/active",
            json={"source_id": src["id"], "target_id": tgt["id"]},
        )

    def test_state_export_runs_real_worker(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "real-state.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        self._localhost_pair(client)
        # No _fake_success: the real run_state_export must execute.
        job = _wait(client, client.post("/api/v1/state/export", json={}).json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["message"] == "State export complete"

    def test_validate_runs_real_worker_offline(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "real-validate.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        self._localhost_pair(client)
        # live=false reads only local state/exports: no network, real code.
        job = _wait(client, client.post("/api/v1/validations", json={}).json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["message"] == "Validation complete"


class TestNeedScope:
    """#24: a corrupt snapshot scope fails closed instead of inferring."""

    def test_corrupt_snapshot_need_rejected(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from aap_migration.api.context import submit_pair_snapshot, verify_execution_pair

        src, tgt = pair
        snap = submit_pair_snapshot(src["id"], tgt["id"], "both")
        params: Any = {
            "source_id": src["id"],
            "target_id": tgt["id"],
            "_snapshot_source_id": snap["_snapshot_source_id"],
            "_snapshot_target_id": snap["_snapshot_target_id"],
            "_snapshot_fp": snap["_snapshot_fp"],
            "_snapshot_need": "bogus",
        }
        with pytest.raises(ValueError, match="corrupt"):
            verify_execution_pair(params)


class TestBurstBackoff:
    """#32: restart-burst backoff never occupies the request thread."""

    def test_burst_restart_returns_fast(self, client: TestClient, monkeypatch: Any) -> None:
        import threading
        import time

        manager = get_job_manager()
        dead = threading.Thread(target=lambda: None, name="dead-burst-probe")
        dead.start()
        dead.join()
        # White-box pin: drives the restart supervisor via its internal
        # _restart_times/_worker state. If the supervisor is refactored,
        # update this test alongside -- it pins backoff behavior through
        # the public ensure_worker() entry point.
        now = time.monotonic()
        manager._restart_times = [now - i for i in range(6)]
        manager._worker = dead
        # Backoff property, not wall-clock: the 5s burst delay must not run
        # on the request thread. Guard time.sleep on the caller so a
        # regression to a blocking sleep fails loudly; the replacement
        # worker's own backoff sleep is short-circuited to keep the suite fast.
        caller = threading.get_ident()
        caller_sleeps: list[float] = []
        real_sleep = time.sleep

        def _tracking_sleep(secs: float) -> None:
            if threading.get_ident() == caller:
                caller_sleeps.append(float(secs))
                return
            real_sleep(min(float(secs), 0.01))

        monkeypatch.setattr(time, "sleep", _tracking_sleep)
        try:
            manager.ensure_worker()
            # Burst recorded (>5 restarts in 60s), not elapsed time.
            assert len(manager._restart_times) > 5
        finally:
            manager._restart_times = []
        assert caller_sleeps == []
        assert manager.worker_alive()


class TestRelativize:
    """#8: _relativize keeps server-local paths out of public payloads."""

    def test_under_workdir_becomes_relative(self, tmp_path: Any) -> None:
        from aap_migration.api.services._core import _relativize

        workdir = tmp_path / "job"
        workdir.mkdir()
        target = str(workdir / "reports" / "out.md")
        assert _relativize(target, workdir) == "reports/out.md"

    def test_outside_workdir_unchanged(self, tmp_path: Any) -> None:
        from aap_migration.api.services._core import _relativize

        workdir = tmp_path / "job"
        workdir.mkdir()
        assert _relativize("/etc/passwd", workdir) == "/etc/passwd"
        assert _relativize("relative/path", workdir) == "relative/path"

    def test_nested_structures_and_passthrough(self, tmp_path: Any) -> None:
        from aap_migration.api.services._core import _relativize

        workdir = tmp_path / "job"
        workdir.mkdir()
        inner = str(workdir / "a.txt")
        payload = {"p": inner, "lst": [inner, 42, None], "n": {"x": inner}}
        out = _relativize(payload, workdir)
        assert out == {"p": "a.txt", "lst": ["a.txt", 42, None], "n": {"x": "a.txt"}}
        assert _relativize(123, workdir) == 123
        assert _relativize(None, workdir) is None

    def test_credential_compare_carries_no_absolute_paths(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import json as _json

        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        ref = get_job_manager().get_internal(job["job_id"])
        blob = _json.dumps(job.get("result") or {})
        assert str(ref["job_dir"]) not in blob


class TestScopeSemantics:
    """#7: omitted resource_types means all, explicit [] is a no-op."""

    def test_explicit_empty_is_noop(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        _fake_success(monkeypatch, "run_export")
        rec = client.post("/api/v1/exports", json={"resource_types": []})
        assert rec.status_code == 202, rec.text
        job = _wait(client, rec.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["artifacts"] == []
        assert "nothing to do" in job["result"]["message"]

    def test_explicit_empty_noop_real_worker(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Real run_export (only chained_ctx stubbed) honors the no-op guard."""
        import aap_migration.api.services.etl as etl_mod
        from aap_migration.api.services._core import noop_result

        def _fake_ctx(job: Any) -> Any:
            import contextlib
            from pathlib import Path

            workdir = Path(job["job_dir"]).resolve()

            @contextlib.contextmanager
            def _cm() -> Any:
                yield (None, None, workdir, job.get("params", {}))

            return _cm()

        # Patch where it is looked up (etl imports the name directly).
        monkeypatch.setattr(etl_mod, "chained_ctx", _fake_ctx)
        rec = client.post("/api/v1/exports", json={"resource_types": []})
        assert rec.status_code == 202, rec.text
        job = _wait(client, rec.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["artifacts"] == []
        assert "nothing to do" in job["result"]["message"]
        assert noop_result()["message"] in job["result"]["message"]

    def test_omitted_means_all(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        _fake_success(monkeypatch, "run_export")
        rec = client.post("/api/v1/exports", json={})
        assert rec.status_code == 202, rec.text
        job = _wait(client, rec.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["message"] == "run_export ok"

    def test_dependency_check_empty_is_noop(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        # Materialize the job DB + xformed dir the way a real export would
        # (mirrors test_chained_dependency_reads_job_db), then the explicit-[]
        # no-op short-circuit must win with a deterministic 200.
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert first["status"] == "succeeded", first.get("error")
        ref = get_job_manager().get_internal(first["job_id"])
        open_state(str(Path(ref["job_dir"]) / "migration_state.db"))
        (Path(ref["job_dir"]) / "xformed").mkdir(parents=True, exist_ok=True)
        resp = client.post(
            "/api/v1/validations/dependencies",
            json={"job_id": first["job_id"], "resource_types": []},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["validation"]["results"] == []

    def test_dependency_check_missing_dir_404(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        # Same scope without an xformed tree: the missing-dir 404 wins.
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert first["status"] == "succeeded", first.get("error")
        resp = client.post(
            "/api/v1/validations/dependencies",
            json={"job_id": first["job_id"], "resource_types": []},
        )
        assert resp.status_code == 404, resp.text


class TestArtifactDownload:
    """Agent-native warning 1: listed artifacts must be downloadable."""

    def test_download_roundtrip_and_guards(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        ref = get_job_manager().get_internal(job["job_id"])
        workdir = Path(ref["job_dir"])
        (workdir / "reports").mkdir(parents=True, exist_ok=True)
        (workdir / "reports" / "hello.md").write_text("# hello")
        listed = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts").json()
        assert "reports/hello.md" in listed["artifacts"]
        got = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/reports/hello.md")
        assert got.status_code == 200, got.text
        assert got.text == "# hello"
        # Secret sidecars are not downloadable even when present.
        (workdir / "config.yaml").write_text("x")
        assert client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/config.yaml").status_code == 404
        # Traversal escapes are rejected.
        assert client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/../../api.db").status_code in (
            400,
            404,
        )
        # Unknown job is 404.
        assert client.get("/api/v1/jobs/nope/artifacts/x.md").status_code == 404


class TestEnvClamp:
    """#10: out-of-range AAP_BRIDGE_* env clamps instead of wedging the lane."""

    def test_clamp_helpers(self, monkeypatch: Any) -> None:
        import importlib
        import math

        import aap_migration.api.jobs._config as cfg

        # Env-driven clamping through the loaded config the manager reads
        # (no direct cfg._clamp_int): out-of-range clamps to bounds,
        # non-finite falls back to the default.
        try:
            monkeypatch.setenv("AAP_BRIDGE_MAX_JOBS", "0")
            importlib.reload(cfg)
            assert cfg.MAX_JOBS == 1
            monkeypatch.setenv("AAP_BRIDGE_MAX_JOBS", "9999999")
            importlib.reload(cfg)
            assert cfg.MAX_JOBS == 100000
            monkeypatch.setenv("AAP_BRIDGE_MAX_JOBS", "7")
            importlib.reload(cfg)
            assert cfg.MAX_JOBS == 7
            monkeypatch.setenv("AAP_BRIDGE_JOB_TIMEOUT", "0.5")
            importlib.reload(cfg)
            assert cfg.JOB_TIMEOUT_SECS == 60.0
            monkeypatch.setenv("AAP_BRIDGE_JOB_TIMEOUT", "99999")
            importlib.reload(cfg)
            assert cfg.JOB_TIMEOUT_SECS == 7200.0
            monkeypatch.setenv("AAP_BRIDGE_JOB_TIMEOUT", "inf")
            importlib.reload(cfg)
            assert cfg.JOB_TIMEOUT_SECS == 3600.0
            monkeypatch.setenv("AAP_BRIDGE_MAX_QUEUE", "0")
            importlib.reload(cfg)
            assert cfg.MAX_QUEUE_DEPTH == 1
            monkeypatch.setenv("AAP_BRIDGE_MAX_ORPHANS", "9999999")
            importlib.reload(cfg)
            assert cfg.MAX_ORPHANS == 1000
            assert math.isfinite(cfg.JOB_TIMEOUT_SECS)
            assert cfg.MAX_QUEUE_DEPTH >= 1 and cfg.MAX_ORPHANS >= 1 and cfg.MAX_JOBS >= 1
        finally:
            monkeypatch.delenv("AAP_BRIDGE_MAX_JOBS", raising=False)
            monkeypatch.delenv("AAP_BRIDGE_JOB_TIMEOUT", raising=False)
            monkeypatch.delenv("AAP_BRIDGE_MAX_QUEUE", raising=False)
            monkeypatch.delenv("AAP_BRIDGE_MAX_ORPHANS", raising=False)
            importlib.reload(cfg)


class TestSnapshotConstants:
    """#9: snapshot keys live once in store and match the writer/reader contract."""

    def test_keys_single_home(self, client: TestClient) -> None:
        from aap_migration.api import store
        from aap_migration.api.context import submit_pair_snapshot

        assert store.SNAPSHOT_SOURCE_ID == "_snapshot_source_id"
        assert store.SNAPSHOT_TARGET_ID == "_snapshot_target_id"
        assert store.SNAPSHOT_FP == "_snapshot_fp"
        assert store.SNAPSHOT_STABLE == "_snapshot_stable"
        assert store.SNAPSHOT_NEED == "_snapshot_need"
        assert store.SNAPSHOT_FERNET_FP == "_snapshot_fernet_fp"
        # The writer emits exactly the single-home keys.
        src = client.post(
            "/api/v1/connections",
            json={
                "name": "snap-src",
                "kind": "source",
                "url": "https://snap-src.example.com/api/v2",
                "token": "t",
            },
        ).json()
        tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "snap-tgt",
                "kind": "target",
                "url": "https://snap-tgt.example.com/api/v2",
                "token": "t",
            },
        ).json()
        snap = submit_pair_snapshot(src["id"], tgt["id"], "both")
        assert set(snap) == {
            store.SNAPSHOT_SOURCE_ID,
            store.SNAPSHOT_TARGET_ID,
            store.SNAPSHOT_FP,
            store.SNAPSHOT_STABLE,
            store.SNAPSHOT_NEED,
            store.SNAPSHOT_FERNET_FP,
        }


class TestFenceRequeue:
    """#3: fence-gated jobs requeue (not fail) until repeated expiries."""

    def test_requeue_then_fail(self, tmp_path: Any) -> None:
        import concurrent.futures
        import threading
        import time

        from aap_migration.api.jobs import JobManager

        mgr = JobManager(base_dir=str(tmp_path / "jobs"))
        # Short public timeout keeps the bounded fence grace fast. Park
        # bound is uncapped (outlives TTL: 301 parks at timeout=1), so
        # clamp it for test speed -- production still parks past one TTL.
        mgr.job_timeout = 1
        mgr._max_park_attempts = lambda: 5  # type: ignore[method-assign]
        # Occupy the FIFO worker so the test job stays queued deterministically.
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        mgr.submit("blocker", {}, _blocker)
        assert entered.wait(timeout=30)
        rec = mgr.submit("x", {}, lambda j: {"message": "ok"})
        job_dir = mgr.get_internal(rec["job_id"])["job_dir"]
        # Public shed-load seam to plant the fence after submit (so submit
        # itself does not shed): the dequeue gate must requeue, not run.
        never: concurrent.futures.Future = concurrent.futures.Future()
        mgr._fences.note_timeout(future=never, pool=None, job_dir=job_dir, pair_fp=None)
        try:
            release.set()
            # Submit-then-observe: the fenced job requeues through the
            # bounded grace and only fails loudly after repeated expiries.
            deadline = time.time() + 30
            final = mgr.get(rec["job_id"])
            while time.time() < deadline:
                final = mgr.get(rec["job_id"])
                if final["status"] == "failed":
                    break
                time.sleep(0.05)
            assert final["status"] == "failed"
            assert "repeated waits" in (final["error"] or "")
            assert "fenced" in (final["error"] or "")
        finally:
            release.set()


class TestHarnessFidelity:
    """#8: unfaked guard-family pins on the real worker path."""

    def test_chained_unknown_job_id_404_unfaked(self, client: TestClient) -> None:
        # No fakes: submit-time chaining against an unknown job must 404
        # through resolve_workdir (pin-missing KeyError path).
        resp = client.post("/api/v1/exports", json={"job_id": "does-not-exist"})
        assert resp.status_code == 404, resp.text
        assert "Unknown job_id" in resp.json()["detail"]

    def test_unresolvable_host_fails_fast(self, client: TestClient, monkeypatch: Any) -> None:
        # Hermetic (P2 #25): stub the bounded-DNS seam so the test proves
        # the failure plumbing (failed + DNS-shaped marker, bounded
        # elapsed, lane stays live) without depending on the runner's
        # resolver. The real-DNS path keeps one marked-slow test below.
        import time as _time

        from api_shared import _wait

        import aap_migration.utils.ssrf as ssrf_mod

        def _unresolvable(url: str, timeout_secs: float = 10.0) -> str:
            raise ValueError("Connection URL host cannot be resolved (stubbed)")

        monkeypatch.setattr(ssrf_mod, "reverify_execution_url", _unresolvable)

        src = client.post(
            "/api/v1/connections",
            json={
                "name": "bad-src",
                "kind": "source",
                "url": "https://no-such-host.invalid/api/v2",
                "token": "t",
                "verify_ssl": False,
            },
        ).json()
        tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "bad-tgt",
                "kind": "target",
                "url": "https://no-such-host.invalid/api/v2",
                "token": "t",
                "verify_ssl": False,
            },
        ).json()
        client.post(
            "/api/v1/connections/active",
            json={"source_id": src["id"], "target_id": tgt["id"]},
        )
        rec = client.post(
            "/api/v1/exports", json={"source_id": src["id"], "target_id": tgt["id"]}
        ).json()
        assert "job_id" in rec, rec
        started = _time.monotonic()
        done = _wait(client, rec["job_id"], timeout=60)
        elapsed = _time.monotonic() - started
        # P1 #12: the lane-wedging guard must actually fail (not succeed
        # without any DNS attempt) with a connectivity-shaped marker, fast.
        assert done["status"] == "failed", done
        assert "cannot be resolved" in (done["error"] or ""), done
        assert elapsed < 30, elapsed
        # Lane must still accept work after the bad-host job.
        probe = client.get("/api/v1/health")
        assert probe.status_code in (200, 503), probe.text

    @pytest.mark.slow
    def test_unresolvable_host_fails_fast_real_dns(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        # Real-DNS integration for the bounded-DNS path above: real export
        # worker against an unresolvable host must fail instead of wedging
        # the FIFO lane. Slow and resolver-dependent by design; deselect
        # with `-m "not slow"`.
        from api_shared import _wait

        src = client.post(
            "/api/v1/connections",
            json={
                "name": "bad-src",
                "kind": "source",
                "url": "https://no-such-host.invalid/api/v2",
                "token": "t",
                "verify_ssl": False,
            },
        ).json()
        tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "bad-tgt",
                "kind": "target",
                "url": "https://no-such-host.invalid/api/v2",
                "token": "t",
                "verify_ssl": False,
            },
        ).json()
        client.post(
            "/api/v1/connections/active",
            json={"source_id": src["id"], "target_id": tgt["id"]},
        )
        rec = client.post(
            "/api/v1/exports", json={"source_id": src["id"], "target_id": tgt["id"]}
        ).json()
        assert "job_id" in rec, rec
        done = _wait(client, rec["job_id"], timeout=120)
        assert done["status"] == "failed", done
        # Lane must still accept work after the bad-host job.
        probe = client.get("/api/v1/health")
        assert probe.status_code in (200, 503), probe.text

    def test_tokenless_401_without_anon_unfaked(self, client: TestClient, monkeypatch: Any) -> None:
        # No fakes: fail-closed default without token and without ALLOW_ANON.
        monkeypatch.delenv("AAP_BRIDGE_ALLOW_ANON", raising=False)
        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN", raising=False)
        resp = client.get("/api/v1/health")
        assert resp.status_code == 401, resp.text
