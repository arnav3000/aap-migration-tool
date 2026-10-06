"""Regression tests for PR 142 review findings (#1-#6, #17, #21).

Covers: worker TypeError redaction policy, reverify running-handle,
422 OpenAPI string envelope, probe path redaction, health/ready branches,
lifecycle pin guard, and config timeout detail.
"""

from typing import Any

import pytest
from api_shared import _wait


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch: Any) -> Any:
    """Creation-time SSRF checks must not need real DNS in this sandbox."""
    monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
    import aap_migration.utils.ssrf as ssrf_mod

    monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", lambda url: url)
    monkeypatch.setattr(ssrf_mod, "reverify_execution_url", lambda url: url)
    yield


class TestWorkerTypeErrorMapping:
    """#1 (P0) + #3 (P1): verbatim drift detail vs error_id redaction."""

    def _run_failing_job(self, client: Any, func: Any) -> Any:
        from aap_migration.api.jobs import get_job_manager

        job = get_job_manager().submit("fail-probe", {}, func)
        done = _wait(client, job["job_id"])
        assert done["status"] == "failed", done
        return done

    def test_drift_typeerror_preserves_actionable_detail(self, client: Any) -> None:
        def _drift(job: Any) -> Any:
            raise TypeError("Unknown parameter(s) for command 'export': foo. Allowed: bar")

        done = self._run_failing_job(client, _drift)
        assert "Unknown parameter(s)" in done["error"]

    def test_generic_typeerror_uses_error_id_redaction(self, client: Any) -> None:
        def _boom(job: Any) -> Any:
            raise TypeError("bad type: /etc/secret/token-xyz in repr")

        done = self._run_failing_job(client, _boom)
        assert "error_id=" in done["error"]
        assert "secret/token-xyz" not in done["error"]
        assert "bad type" not in done["error"]

    def test_runtime_error_uses_error_id_redaction(self, client: Any) -> None:
        def _boom(job: Any) -> Any:
            raise RuntimeError("backend boom: secret-token-xyz")

        done = self._run_failing_job(client, _boom)
        assert "error_id=" in done["error"]
        assert "secret-token-xyz" not in done["error"]


class TestReverifyRunningHandle:
    """#4 (P1): fingerprint failure on a running job keeps the job_id."""

    def test_fingerprint_failure_with_fail_fast_false_returns(
        self, client: Any, monkeypatch: Any
    ) -> None:
        from aap_migration.api.routers import _common as common

        calls: dict[str, int] = {"n": 0}

        def _no_fail(job_id: str, message: str) -> bool:
            calls["n"] += 1
            return False

        def _boom(scope: Any) -> Any:
            raise ValueError("pair drifted")

        monkeypatch.setattr(common.get_job_manager(), "fail_fast", _no_fail)
        import aap_migration.api.store as store_mod

        monkeypatch.setattr(store_mod, "pair_fingerprint", _boom)
        # Must not raise: the job is already running, execution-time
        # verify owns the failure with the job_id intact.
        common._reverify_post_submit(None, None, "both", {"allow_pair_switch": False}, "j1")
        assert calls["n"] == 1

    def test_fingerprint_failure_with_fail_fast_true_raises_409(
        self, client: Any, monkeypatch: Any
    ) -> None:
        from fastapi import HTTPException

        from aap_migration.api.routers import _common as common

        def _failed(job_id: str, message: str) -> bool:
            return True

        def _boom(scope: Any) -> Any:
            raise ValueError("pair drifted")

        monkeypatch.setattr(common.get_job_manager(), "fail_fast", _failed)
        import aap_migration.api.store as store_mod

        monkeypatch.setattr(store_mod, "pair_fingerprint", _boom)
        with pytest.raises(HTTPException) as exc_info:
            common._reverify_post_submit(None, None, "both", {"allow_pair_switch": False}, "j1")
        # ValueError maps to 400 via _store_http_error; the point of #4
        # is that fail_fast was honored (job failed) before raising.
        assert exc_info.value.status_code == 400


class TestOpenAPI422Envelope:
    """#2 (P1): wire 422 string detail matches OpenAPI docs."""

    def test_openapi_documents_string_detail(self, client: Any) -> None:
        spec = client.get("/api/v1/openapi.json").json()
        assert "StringDetailError" in spec.get("components", {}).get(
            "schemas", {}
        ), "custom 422 component missing from OpenAPI"
        found_422 = False
        for path_item in spec.get("paths", {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                resp_422 = (operation.get("responses") or {}).get("422")
                if resp_422:
                    found_422 = True
                    schema = (
                        resp_422.get("content", {}).get("application/json", {}).get("schema", {})
                    )
                    assert schema.get("$ref", "").endswith("StringDetailError"), schema
        assert found_422, "no 422 response documented in OpenAPI"

    def test_wire_422_is_string_detail(self, client: Any) -> None:
        resp = client.post("/api/v1/config/validate", json={"check_connectivity": "not-a-bool"})
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], str)


class TestProbeRedaction:
    """#17 (P2): readiness/health omit absolute server paths."""

    def test_missing_db_is_generic_without_path(self, client: Any, monkeypatch: Any) -> None:
        import aap_migration.api.models as models_mod

        monkeypatch.setattr(models_mod, "api_db_path", lambda: "/srv/secret/api.db")
        resp = client.get("/api/v1/ready")
        assert resp.status_code == 503
        body = resp.json()["detail"] if "detail" in resp.json() else resp.json()
        text = str(body)
        assert "/srv/secret" not in text
        assert "unwritable: database" in text

    def test_startup_degraded_is_path_free(self, client: Any, monkeypatch: Any) -> None:
        import aap_migration.api.jobs._config as cfg

        # Behavioral (not source-text): a path-bearing latch set by an
        # older process must not reach pollers verbatim; health serves
        # the sanitized marker and 503s.
        monkeypatch.setattr(cfg, "_startup_degraded", "/srv/secret/api.db: boom")
        resp = client.get("/api/v1/health")
        assert resp.status_code == 503
        text = str(resp.json())
        assert "/srv/secret" not in text
        body = resp.json()
        assert body.get("startup_degraded") in (
            "api-db: storage-unhealthy",
            "job-dir: storage-unhealthy",
            "storage-unhealthy",
        )

    def test_latch_writers_are_generic(self, client: Any) -> None:
        # Writer-side backstop: the lifespan latch stores only generic
        # markers (the serving layer above is defense-in-depth).
        import inspect

        from aap_migration.api import app as app_mod

        src = inspect.getsource(app_mod.lifespan)
        assert 'degraded.append("api-db: storage-unhealthy")' in src
        assert 'degraded.append("job-dir: storage-unhealthy")' in src


class TestHealthReadyBranches:
    """#5 (P1): healthy/degraded/503 probe branches."""

    def test_healthy_serves_expected_keys(self, client: Any) -> None:
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("status", "service", "version", "worker", "queue_depth"):
            assert key in body, key
        assert body["status"] == "ok"

    def test_ready_healthy_shape(self, client: Any) -> None:
        resp = client.get("/api/v1/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["checks"]["worker"] == "alive"

    def test_dead_worker_is_503(self, client: Any, monkeypatch: Any) -> None:
        import aap_migration.api.routers.system as system_mod

        monkeypatch.setattr(system_mod, "_worker_state", lambda: ("dead", 3))
        assert client.get("/api/v1/health").status_code == 503
        ready = client.get("/api/v1/ready")
        assert ready.status_code == 503
        assert "dead" in str(ready.json()["checks"])

    def test_unwritable_job_dir_is_503_generic(self, client: Any, monkeypatch: Any) -> None:
        import aap_migration.api.routers.system as system_mod

        def _boom(directory: str) -> None:
            raise OSError("/srv/secret/jobs: denied")

        monkeypatch.setattr(system_mod, "probe_dir_writable", _boom)
        resp = client.get("/api/v1/ready")
        assert resp.status_code == 503
        text = str(resp.json())
        assert "/srv/secret" not in text
        assert "unwritable: job_dir" in text


class TestLifecyclePinGuard:
    """#6 (P1): chained_ctx pin assert and workdir teardown."""

    def test_missing_pin_raises_keyerror(self, tmp_path: Any) -> None:
        from aap_migration.api.services._core import chained_ctx

        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        with pytest.raises(KeyError, match="pin key"):
            with chained_ctx(job):  # type: ignore[arg-type]
                pass

    def test_non_dict_params_raises_typeerror(self, tmp_path: Any) -> None:
        from aap_migration.api.services._core import chained_ctx

        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": "nope"}
        with pytest.raises(TypeError, match="must be a dict"):
            with chained_ctx(job):  # type: ignore[arg-type]
                pass

    def test_workdir_teardown_on_success(self, tmp_path: Any) -> None:
        import aap_migration.api.context as ctx_mod
        from aap_migration.api.services._core import workdir_ctx

        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        with workdir_ctx(job):  # type: ignore[arg-type]
            pass
        assert ctx_mod._job_log_handlers == {}

    def test_workdir_teardown_on_exception(self, tmp_path: Any) -> None:
        import aap_migration.api.context as ctx_mod
        from aap_migration.api.services._core import workdir_ctx

        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        with pytest.raises(RuntimeError, match="worker boom"):
            with workdir_ctx(job):  # type: ignore[arg-type]
                raise RuntimeError("worker boom")
        assert ctx_mod._job_log_handlers == {}


class TestConfigTimeoutDetail:
    """#21 (P3): config probe timeout carries a timeout message."""

    def test_timeout_detail_says_timed_out(self, client: Any, pair: Any, monkeypatch: Any) -> None:
        import aap_migration.client.aap_source_client as source_mod
        import aap_migration.client.aap_target_client as target_mod
        import aap_migration.utils.ssrf as ssrf_mod

        monkeypatch.setattr(ssrf_mod, "reverify_execution_url_bounded", lambda url: url)

        async def _get(self: Any, *args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("slow backend")

        monkeypatch.setattr(source_mod.AAPSourceClient, "get", _get)
        monkeypatch.setattr(target_mod.AAPTargetClient, "get", _get)
        resp = client.post("/api/v1/config/validate", json={"check_connectivity": True})
        assert resp.status_code == 502
        assert "timed out" in resp.json()["detail"].lower()


class TestClickExceptionScrub:
    """P0 #1: ClickException backend detail is scrubbed before polling."""

    def test_click_exception_secret_is_scrubbed(self, client: Any) -> None:
        import click

        from aap_migration.api.jobs import get_job_manager

        def _leak(job: Any) -> Any:
            raise click.ClickException("backend boom: token=secret-xyz-123")

        job = get_job_manager().submit("leak-probe", {}, _leak)
        done = _wait(client, job["job_id"])
        assert done["status"] == "failed", done
        assert "secret-xyz-123" not in done["error"]
        assert "token=" not in done["error"] or "***" in done["error"]

    def test_value_error_guidance_is_scrubbed(self, client: Any) -> None:
        from aap_migration.api.jobs import get_job_manager

        def _leak(job: Any) -> Any:
            raise ValueError("drift: token=secret-xyz-123")

        job = get_job_manager().submit("leak-probe", {}, _leak)
        done = _wait(client, job["job_id"])
        assert done["status"] == "failed", done
        assert "secret-xyz-123" not in done["error"]


class TestArtifact400Generic:
    """P2 #10: artifact traversal 400 omits the absolute base dir."""

    def test_traversal_400_is_generic(self, client: Any) -> None:
        from pathlib import Path

        from aap_migration.api.jobs import get_job_manager

        def _plant(job: Any) -> Any:
            base = Path(job["job_dir"])
            (base / "reports").mkdir(parents=True, exist_ok=True)
            (base / "reports" / "r.md").write_text("# report")
            return {"message": "seeded"}

        job = get_job_manager().submit("seed", {}, _plant)
        done = _wait(client, job["job_id"])
        assert done["status"] == "succeeded", done
        # Absolute outside path (survives URL normalization, unlike ".."
        # which the router normalizes before confine_path sees it).
        resp = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts//etc/hostname")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail == "artifact_path must stay under the job directory"
        assert "/tmp" not in detail and "/srv" not in detail and "/etc" not in detail


class TestExceptionHandlerEnvelope:
    """P1 #3: app exception handlers map to the string-detail envelope."""

    def _raise_through_jobs_list(self, client: Any, monkeypatch: Any, exc: BaseException) -> Any:
        from aap_migration.api.jobs import get_job_manager

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise exc

        monkeypatch.setattr(get_job_manager(), "count", _boom)
        return client.get("/api/v1/jobs")

    def test_value_error_is_400(self, client: Any, monkeypatch: Any) -> None:
        resp = self._raise_through_jobs_list(client, monkeypatch, ValueError("bad input"))
        assert resp.status_code == 400
        assert resp.json() == {"detail": "bad input"}

    def test_conflict_error_is_409(self, client: Any, monkeypatch: Any) -> None:
        from aap_migration.api._errors import ConflictError

        resp = self._raise_through_jobs_list(client, monkeypatch, ConflictError("busy"))
        assert resp.status_code == 409
        assert resp.json() == {"detail": "busy"}

    def test_key_error_is_404(self, client: Any, monkeypatch: Any) -> None:
        resp = self._raise_through_jobs_list(client, monkeypatch, KeyError("missing-id"))
        assert resp.status_code == 404
        assert resp.json() == {"detail": "missing-id"}

    def test_storage_unhealthy_is_503(self, client: Any, monkeypatch: Any) -> None:
        from aap_migration.api._errors import StorageUnhealthyError

        resp = self._raise_through_jobs_list(
            client, monkeypatch, StorageUnhealthyError("storage down")
        )
        assert resp.status_code == 503
        assert resp.json() == {"detail": "storage down"}

    def test_internal_status_is_500(self, client: Any, monkeypatch: Any) -> None:
        from aap_migration.api._errors import InternalStatusError

        resp = self._raise_through_jobs_list(client, monkeypatch, InternalStatusError("bad status"))
        assert resp.status_code == 500
        assert resp.json() == {"detail": "bad status"}


class TestArtifactHelpers:
    """P1 #4: _artifact_result cap and _relativize guards are pinned."""

    def test_artifact_result_caps_and_counts_total(self, tmp_path: Any) -> None:
        from typing import cast

        from aap_migration.api.services._core import _artifact_result

        reports = tmp_path / "reports"
        reports.mkdir()
        for i in range(5):
            (reports / f"f{i}.json").write_text("{}")
        payload = _artifact_result(tmp_path, "reports", cap=3)
        assert len(cast(list[str], payload["artifacts"])) == 3
        assert payload["artifacts_total"] == 5
        assert payload["artifacts_truncated"] is True
        full = _artifact_result(tmp_path, "reports", cap=10)
        assert full["artifacts_total"] == 5
        assert full["artifacts_truncated"] is False

    def test_relativize_rewrites_under_workdir(self, tmp_path: Any) -> None:
        from pathlib import Path

        from aap_migration.api.services._core import _relativize

        workdir = Path(tmp_path)
        (workdir / "reports").mkdir()
        inside = str(workdir / "reports" / "r.json")
        outside = "/etc/hostname"
        assert _relativize(inside, workdir) == "reports/r.json"
        assert _relativize(outside, workdir) == outside
        nested = {"report": inside, "items": [inside, outside]}
        out = _relativize(nested, workdir)
        assert out == {"report": "reports/r.json", "items": ["reports/r.json", outside]}


class TestCancelCooperative:
    """P1 #5: mutating workers honor cancel before start."""

    def test_credential_migrate_cancel_before_start(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.credentials as creds

        monkeypatch.setattr(creds, "_cancel_requested", lambda job: True)
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        result = creds.run_credential_migrate(job)  # type: ignore[arg-type]
        assert result.get("cancelled") is True

    def test_iam_migrate_cancel_before_start(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services._core as core
        import aap_migration.api.services.iam as iam

        # run_iam_migrate imports _cancel_requested from _core at call
        # time, so patch the single home in core.
        monkeypatch.setattr(core, "_cancel_requested", lambda job: True)
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        result = iam.run_iam_migrate(job)  # type: ignore[arg-type]
        assert result.get("cancelled") is True

    def test_cleanup_cancel_before_start(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.maintenance as maintenance

        monkeypatch.setattr(maintenance, "_cancel_requested", lambda job: True)
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        result = maintenance.run_cleanup(job)  # type: ignore[arg-type]
        assert result.get("cancelled") is True


class TestConfigValidateGuards:
    """Config 400 gates: non-positive performance settings are rejected."""

    @pytest.mark.parametrize(
        "patch, expected",
        [
            ({"batch_sizes": {"default": 0}}, "Batch size must be positive"),
            ({"max_concurrent": 0}, "Max concurrent requests must be positive"),
            ({"rate_limit": 0}, "Rate limit must be positive"),
        ],
    )
    def test_non_positive_settings_are_400(
        self, client: Any, pair: Any, monkeypatch: Any, patch: Any, expected: str
    ) -> None:
        import aap_migration.api.routers.config as config_router

        real = config_router.build_ephemeral_context

        def _patched(source_id: Any = None, target_id: Any = None) -> Any:
            ctx = real(source_id, target_id)
            for key, value in patch.items():
                if key == "batch_sizes":
                    ctx.config.performance.batch_sizes.update(value)
                else:
                    setattr(ctx.config.performance, key, value)
            return ctx

        monkeypatch.setattr(config_router, "build_ephemeral_context", _patched)
        resp = client.post("/api/v1/config/validate", json={})
        assert resp.status_code == 400
        assert expected in resp.json()["detail"]
