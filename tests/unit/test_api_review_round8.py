"""Regression tests for ce-code-review PR #143 (round 8).

Covers validated findings #1 (P0 ClickException scrub), #6 (artifact 400
generic), #8 (readiness generic), #9 (targetless job-scoped reads) and
report-only #3 (reset helper), #4 (state-import size bound), #7 (shared
preamble helper). Findings #2 and #12 were rejected by the validator and
are intentionally not pinned.
"""

from typing import Any

from fastapi.testclient import TestClient


def _allow_private(monkeypatch: Any) -> None:
    # Sandbox has no DNS: bypass SSRF DNS resolution for example.com hosts
    # (CI resolves them; sandbox fails closed). Private-IP allowance skips
    # the bounded resolve in validate_connection_url.
    monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")


def _make_pair(client: TestClient) -> tuple:
    src = client.post(
        "/api/v1/connections",
        json={
            "name": "src",
            "kind": "source",
            "url": "https://src.example.com/api/v2",
            "token": "s3cret",
            "verify_ssl": False,
        },
    ).json()
    tgt = client.post(
        "/api/v1/connections",
        json={
            "name": "tgt",
            "kind": "target",
            "url": "https://tgt.example.com/api/controller/v2",
            "token": "t0p",
            "verify_ssl": False,
        },
    ).json()
    client.post(
        "/api/v1/connections/active",
        json={"source_id": src["id"], "target_id": tgt["id"]},
    )
    return src, tgt


class TestClickExceptionScrubbed:
    """#1 P0: ClickException backend detail must be scrubbed before polling."""

    def test_click_exception_token_scrubbed(self, client: TestClient, monkeypatch: Any) -> None:
        from api_shared import _wait

        from aap_migration.api.jobs import get_job_manager

        _allow_private(monkeypatch)
        # Submit a job whose worker raises ClickException carrying a
        # token-like secret, mirroring CLI ClickException(str(e)) wrapping.
        import click

        def _leaky(job: Any) -> Any:
            raise click.ClickException("failed for token=supersecret12345")

        manager = get_job_manager()
        rec = manager.submit("leaky", {}, _leaky)
        result = _wait(client, rec["job_id"])
        assert result["status"] == "failed", result
        error = result.get("error", "")
        assert "supersecret12345" not in error, error
        # Scrubbed marker preserved (pattern replaces value with ***).
        assert "***" in error or error == "Job failed" or "token" in error.lower(), error


class TestArtifactConfineGeneric:
    """#6 P2: artifact traversal 400 must not embed absolute base dir."""

    def test_artifact_traversal_generic_detail(self, client: TestClient, monkeypatch: Any) -> None:
        from api_shared import _fake_success, _wait

        from aap_migration.api.jobs import get_job_manager

        _allow_private(monkeypatch)
        _make_pair(client)
        _fake_success(monkeypatch, "run_export")
        jid = client.post("/api/v1/exports", json={}).json()["job_id"]
        assert _wait(client, jid)["status"] == "succeeded"
        job_dir = get_job_manager().get_internal(jid)["job_dir"]
        # Encoded traversal survives client-side normalization and reaches
        # confine_path as ../other (absolute base must not leak).
        resp = client.get(f"/api/v1/jobs/{jid}/artifacts/%2e%2e%2fother")
        assert resp.status_code == 400, resp.text
        # Generic detail, no absolute server path.
        assert resp.json()["detail"] == "artifact_path must stay under the job directory"
        assert job_dir not in resp.text


class TestReadinessGeneric:
    """#8 P2: /ready checks must not embed absolute filesystem paths."""

    def test_ready_missing_db_generic(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        missing = str(tmp_path / "no-such-dir" / "api.db")
        monkeypatch.setenv("AAP_BRIDGE_API_DB", missing)
        # Point job dir at an existing writable dir so only DB fails.
        monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", str(tmp_path))
        resp = client.get("/api/v1/ready")
        assert resp.status_code in (200, 503), resp.text
        body = resp.json()
        checks = body.get("checks", {})
        db_check = str(checks.get("database", ""))
        # Generic marker, no absolute path.
        assert "unwritable" in db_check or db_check == "writable", body
        if "unwritable" in db_check:
            assert str(tmp_path) not in resp.text, resp.text
            assert "no-such-dir" not in resp.text, resp.text


class TestTargetlessJobScopedReads:
    """#9 P2: source-only deployments with job_id must not 400 for target."""

    def test_validation_job_scoped_source_only(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from api_shared import _fake_success, _wait

        from aap_migration.api.context import open_state

        _allow_private(monkeypatch)
        # Full pair first so the export job can be created (need=both),
        # then switch to source-only before the targetless read.
        _make_pair(client)
        # Seed default state + transform dir so server-default would 200.
        db = str(tmp_path / "state.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        active = client.get("/api/v1/connections/active").json()
        client.post(
            "/api/v1/connections/active",
            json={"source_id": active["source_id"], "target_id": None, "clear_target": True},
        )
        # Job-scoped dependency check without explicit ids must not 400
        # for a missing target; it judges chained state (200 or 404).
        resp = client.post("/api/v1/validations/dependencies", json={"job_id": job["job_id"]})
        assert resp.status_code in (200, 404), resp.text
        assert "No target AAP configured" not in resp.text

    def test_migrations_job_scoped_source_only(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from api_shared import _fake_success, _wait

        from aap_migration.api.context import open_state

        _allow_private(monkeypatch)
        _make_pair(client)
        db = str(tmp_path / "state2.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        active = client.get("/api/v1/connections/active").json()
        client.post(
            "/api/v1/connections/active",
            json={"source_id": active["source_id"], "target_id": None, "clear_target": True},
        )
        resp = client.post("/api/v1/imports/check-dependencies", json={"job_id": job["job_id"]})
        assert resp.status_code in (200, 404), resp.text
        assert "No target AAP configured" not in resp.text


class TestStateImportSizeBound:
    """#4 P2: oversize guard branches pinned (413/env-fallback/OSError)."""

    def _make_job_with_backup(
        self, client: TestClient, monkeypatch: Any, name: str = "backup.json"
    ) -> str:
        from api_shared import _fake_success, _wait

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        _make_pair(client)
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        ref = get_job_manager().get_internal(job["job_id"])
        # Ensure a state DB exists so import reaches the size gate (not 404).
        open_state(str(__import__("pathlib").Path(ref["job_dir"]) / "migration_state.db"))
        return str(job["job_id"])

    def test_oversize_backup_413(self, client: TestClient, monkeypatch: Any, tmp_path: Any) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        _allow_private(monkeypatch)
        db = str(tmp_path / "s.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        jid = self._make_job_with_backup(client, monkeypatch)
        ref = get_job_manager().get_internal(jid)
        backup = Path(ref["job_dir"]) / "big.json"
        backup.write_bytes(b"x" * 100)
        monkeypatch.setenv("AAP_BRIDGE_MAX_STATE_IMPORT_BYTES", "10")
        # Job-scoped so confine uses the job dir as base (file lives there).
        resp = client.post("/api/v1/state/import", json={"state_file": "big.json", "job_id": jid})
        assert resp.status_code == 413, resp.text
        assert "State backup exceeds" in resp.json()["detail"]

    def test_malformed_override_falls_back(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        _allow_private(monkeypatch)
        db = str(tmp_path / "s2.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        jid = self._make_job_with_backup(client, monkeypatch)
        ref = get_job_manager().get_internal(jid)
        (Path(ref["job_dir"]) / "ok.json").write_text("{}")
        monkeypatch.setenv("AAP_BRIDGE_MAX_STATE_IMPORT_BYTES", "not-a-number")
        resp = client.post("/api/v1/state/import", json={"state_file": "ok.json", "job_id": jid})
        # Malformed override falls back to 64MiB: must not 500, must not 413
        # for a tiny file (import proceeds to 200/404/409, never 500).
        assert resp.status_code != 500, resp.text
        assert resp.status_code != 413, resp.text

    def test_stat_oserror_404(self, client: TestClient, monkeypatch: Any, tmp_path: Any) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        _allow_private(monkeypatch)
        db = str(tmp_path / "s3.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        jid = self._make_job_with_backup(client, monkeypatch)
        ref = get_job_manager().get_internal(jid)
        (Path(ref["job_dir"]) / "gone.json").write_text("{}")
        _orig_stat = Path.stat

        def _flaky_stat(self: Any, *a: Any, **k: Any) -> Any:
            if self.name == "gone.json":
                raise OSError("gone")
            return _orig_stat(self, *a, **k)

        monkeypatch.setattr(Path, "stat", _flaky_stat)
        resp = client.post("/api/v1/state/import", json={"state_file": "gone.json", "job_id": jid})
        assert resp.status_code == 404, resp.text


class TestResetHelperParity:
    """#3 P2: single-home reset helper keeps envelopes identical."""

    def test_reset_envelope_parity_job_vs_default(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from api_shared import _fake_success, _wait

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager

        _allow_private(monkeypatch)
        _make_pair(client)
        db = str(tmp_path / "reset.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        # Ensure job has isolated DB.
        open_state(
            str(
                __import__("pathlib").Path(get_job_manager().get_internal(job["job_id"])["job_dir"])
                / "migration_state.db"
            )
        )
        scoped = client.post(
            "/api/v1/state/reset",
            json={"job_id": job["job_id"], "resource_type": "organizations"},
        )
        assert scoped.status_code == 200, scoped.text
        default = client.post("/api/v1/state/reset", json={"resource_type": "organizations"})
        assert default.status_code == 200, default.text
        assert set(scoped.json()) == set(default.json())
        assert scoped.json()["resource_type"] == default.json()["resource_type"] == "organizations"


class TestSharedPreambleHelper:
    """#7 P2: both dependency readers share resolve_chained_scope."""

    def test_both_routers_use_shared_helper(self) -> None:
        import inspect

        import aap_migration.api.routers.migrations as mig
        import aap_migration.api.routers.validation as val
        from aap_migration.api.routers import _common

        assert hasattr(_common, "resolve_chained_scope")
        mig_src = inspect.getsource(mig.check_import_dependencies)
        val_src = inspect.getsource(val.check_dependencies)
        assert "resolve_chained_scope" in mig_src, "migrations must use shared helper"
        assert "resolve_chained_scope" in val_src, "validation must use shared helper"
        # No direct build_ephemeral_context in either reader (single home).
        assert "build_ephemeral_context" not in mig_src
        assert "build_ephemeral_context" not in val_src
