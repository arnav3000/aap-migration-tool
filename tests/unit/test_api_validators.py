"""REST API tests: validators, SSRF, confinement, pinning, hardening."""

from typing import Any

import pytest
from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient

from aap_migration.api.jobs import get_job_manager


class TestSyncValidators:
    def test_payload_check(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/validations/payload",
            json={"resource_type": "organizations", "payload": {"name": "Default"}},
        )
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_schema_validator_rejections(self, pair: Any, client: TestClient) -> None:
        """Invalid benchmark widths and contradictory scopes fail fast with 422."""
        assert client.post("/api/v1/iam/benchmark", json={"workers": [0]}).status_code == 422
        assert client.post("/api/v1/iam/benchmark", json={"workers": [65]}).status_code == 422
        resp = client.post(
            "/api/v1/analysis/dependencies",
            json={"analyze_all": True, "organizations": ["x"]},
        )
        assert resp.status_code == 422, resp.text
        resp = client.post("/api/v1/iam/report", json={})
        assert resp.status_code == 422, resp.text
        assert "json_path" in resp.text or "job_id" in resp.text

    def test_transform_preview(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/transforms/preview",
            json={"resource_type": "organizations", "payload": {"id": 1, "name": "D"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        # One envelope on both branches: callers branch on valid.
        assert body["preview"] is True
        assert body["valid"] is True
        assert body["errors"] == []
        assert "transformed" in body

    def test_transform_preview_unknown_type(self, client: TestClient) -> None:
        # Unknown types 404 like GET /resources/{type} (contract pin).
        resp = client.post(
            "/api/v1/transforms/preview",
            json={"resource_type": "nope", "payload": {}},
        )
        assert resp.status_code == 404
        assert "Unknown resource type" in resp.json()["detail"]

    def test_transform_preview_error_branches(self, client: TestClient, monkeypatch: Any) -> None:
        from aap_migration.migration import transformer as transformer_mod
        from aap_migration.migration.transformer import SkipResourceError

        class _MissingDep:
            def transform_resource(self, resource_type: Any, payload: Any) -> Any:
                raise SkipResourceError(
                    "missing org",
                    resource_type=resource_type,
                    source_id=1,
                    missing_dependency="organizations",
                )

        monkeypatch.setattr(transformer_mod, "create_transformer", lambda *a, **k: _MissingDep())
        resp = client.post(
            "/api/v1/transforms/preview",
            json={"resource_type": "job_templates", "payload": {"name": "x"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert body["errors"] and "organizations" in body["errors"][0]
        assert body["transformed"] is None
        assert body["preview"] is True

        def _raise_notimpl(*a: Any, **k: Any) -> Any:
            raise NotImplementedError("no transformer for this type")

        monkeypatch.setattr(transformer_mod, "create_transformer", _raise_notimpl)
        resp = client.post(
            "/api/v1/transforms/preview",
            json={"resource_type": "organizations", "payload": {}},
        )
        assert resp.status_code == 400

    def test_state_empty(self, client: TestClient) -> None:
        show = client.get("/api/v1/state/show").json()
        # Isolated STARTUP_CWD has no DB: the empty shape uses "warning",
        # never the "detail" error envelope.
        assert show["migration_id"] is None
        assert "warning" in show
        assert "detail" not in show
        assert client.get("/api/v1/state/mappings").json()["mappings"] == []

    def test_state_import_confinement(self, pair: Any, client: TestClient) -> None:
        # Traversal outside the job tree is rejected, not opened.
        resp = client.post("/api/v1/state/import", json={"state_file": "../../etc/passwd"})
        assert resp.status_code == 400
        # Query-param shape is gone (Pydantic body with 422 on unknown keys).
        assert client.post("/api/v1/state/import", json={"bogus": 1}).status_code == 422

    def test_checkpoints_reject_unknown_keys(self, client: TestClient) -> None:
        assert client.post("/api/v1/checkpoints", json={"bogus_key": 1}).status_code == 422

    def test_config_show_validate(self, pair: Any, client: TestClient) -> None:
        assert client.post("/api/v1/config/show", json={}).status_code == 200
        body = client.post("/api/v1/config/validate", json={}).json()
        assert body["valid"] is True


class TestSsrfGuards:
    """Literal, encoded, and strict-mode SSRF branches (#9)."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://metadata.goog/",
            "http://[::ffff:169.254.169.254]/",
            "http://2852039166/",  # decimal 169.254.169.254
            "http://0xa9.0xfe.0xa9.0xfe/",  # hex 169.254.169.254
            "http://[fe80::1]/",
            "http://[fd00::1]/",
            "http://100.100.100.200/",
        ],
    )
    def test_metadata_spellings_blocked(self, url: str) -> None:
        from aap_migration.utils.ssrf import is_metadata_url, validate_connection_url

        assert is_metadata_url(url) is True
        with pytest.raises(ValueError):
            validate_connection_url(url)

    def test_benign_urls_pass(self) -> None:
        from aap_migration.utils.ssrf import is_metadata_url, validate_connection_url

        assert is_metadata_url("https://src.example.com/api/v2") is False
        assert (
            validate_connection_url("https://aap.example.com/api/v2")
            == "https://aap.example.com/api/v2"
        )
        # Private AAP hosts stay allowed without strict mode (no DNS lookup).
        assert validate_connection_url("https://192.168.1.10/api/v2").startswith("https://")

    def test_strict_mode_blocks_private(self, client: TestClient, monkeypatch: Any) -> None:
        import socket

        from aap_migration.utils.ssrf import validate_connection_url

        monkeypatch.setenv("AAP_BRIDGE_SSRF_STRICT", "1")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))],
        )
        with pytest.raises(ValueError):
            validate_connection_url("https://aap.internal.example.com/api/v2")

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", " True "])
    def test_strict_flag_truthy_spellings(self, monkeypatch: Any, value: str) -> None:
        """Word-form flag values enable strict mode regardless of case."""
        from aap_migration.utils.ssrf import _strict_enabled

        monkeypatch.setenv("AAP_BRIDGE_SSRF_STRICT", value)
        assert _strict_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "2"])
    def test_strict_flag_falsy_spellings(self, monkeypatch: Any, value: str) -> None:
        from aap_migration.utils.ssrf import _strict_enabled

        monkeypatch.setenv("AAP_BRIDGE_SSRF_STRICT", value)
        assert _strict_enabled() is False

    def test_execution_reverify_blocks_metadata(self) -> None:
        from aap_migration.utils.ssrf import reverify_execution_url

        with pytest.raises(ValueError):
            reverify_execution_url("http://169.254.169.254/")
        # Unresolvable hosts fail closed: the job fails with an actionable
        # DNS error instead of delivering the bearer token to a rebound IP.
        with pytest.raises(ValueError, match="cannot be resolved"):
            reverify_execution_url("https://no-such-host.invalid/api")


class TestPathConfinement:
    """Absolute, symlink, cross-job, and iam traversal branches (#10)."""

    def test_absolute_escape_rejected(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        resp = client.post("/api/v1/state/import", json={"state_file": "/etc/passwd"})
        assert resp.status_code == 400

    def test_cross_job_read_requires_job_id(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        ref = get_job_manager().get_internal(first["job_id"])
        victim = f"{ref['job_dir']}/exports/orgs.json"
        resp = client.post("/api/v1/state/import", json={"state_file": victim})
        assert resp.status_code == 400

    def test_iam_json_path_traversal_rejected(self, pair: Any, client: TestClient) -> None:
        # Worker-time user errors are redacted to a generic error_id (server
        # logs keep the detail), so the assertion is terminal failure, not
        # message content; direct confine_path tests below pin the message.
        resp = client.post("/api/v1/iam/report", json={"json_path": "../../etc/passwd"})
        assert resp.status_code == 202, resp.text
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "error_id" in (job["error"] or "")

    def test_iam_missing_file_names_file(self, pair: Any, client: TestClient) -> None:
        # The distinct "not found" message is server-side (worker errors are
        # redacted to error_id); the API contract is terminal failure, while
        # the unit-level distinction lives in run_iam_report itself.
        resp = client.post("/api/v1/iam/report", json={"json_path": "absent.json"})
        assert resp.status_code == 202, resp.text
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "error_id" in (job["error"] or "")

    def test_iam_report_missing_vs_omitted_messages(self, pair: Any, client: TestClient) -> None:
        # #27: omitted input vs confined-but-missing file raise distinct
        # messages so callers can tell them apart (server-side text).
        import aap_migration.api.services as services_mod

        base = get_job_manager().base_dir
        with pytest.raises(ValueError, match="required"):
            services_mod.run_iam_report({"params": {}, "job_dir": base})
        with pytest.raises(ValueError, match="not found"):
            services_mod.run_iam_report({"params": {"json_path": "absent.json"}, "job_dir": base})

    def test_confine_path_branches(self, tmp_path: Any) -> None:
        from aap_migration.api.security import confine_path

        base = tmp_path / "base"
        base.mkdir()
        assert confine_path("sub/file.json", str(base)).name == "file.json"
        with pytest.raises(ValueError):
            confine_path("../escape.json", str(base))
        with pytest.raises(ValueError):
            confine_path("/etc/passwd", str(base))
        secret = tmp_path / "secret.txt"
        secret.write_text("x")
        link = base / "link"
        link.symlink_to(secret)
        with pytest.raises(ValueError):
            confine_path(str(link), str(base))


class TestConnectionPinning:
    """Submit-time snapshots and execution-time drift detection (#3, #7, #11, #14)."""

    def test_kind_swap_rejected(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        src, tgt = pair
        resp = client.post("/api/v1/exports", json={"source_id": tgt["id"]})
        assert resp.status_code == 400
        assert "not a source" in resp.json()["detail"]
        resp = client.post("/api/v1/exports", json={"source_id": src["id"], "target_id": src["id"]})
        assert resp.status_code == 400
        assert "not a target" in resp.json()["detail"]

    def test_token_rotation_fails_execution(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from aap_migration.api import store
        from aap_migration.api.context import verify_execution_pair

        src, tgt = pair
        snap = store.pair_fingerprint(src["id"], tgt["id"])
        params: Any = {
            "source_id": src["id"],
            "target_id": tgt["id"],
            "_snapshot_source_id": snap["source_id"],
            "_snapshot_target_id": snap["target_id"],
            "_snapshot_fp": snap["fp"],
        }
        verify_execution_pair(params)
        store.update_connection(src["id"], token="rotated-token")
        with pytest.raises(ValueError, match="changed since"):
            verify_execution_pair(params)
        # allow_pair_switch covers intentional id switches only: a pure
        # credential rotation under identical ids still fails so planned
        # rotations cannot piggyback on an unrelated switched job.
        params["allow_pair_switch"] = True
        with pytest.raises(ValueError, match="not rotations"):
            verify_execution_pair(params)
        # An actual id switch after a rotation still fails closed: the
        # fingerprint changed, so the job must be resubmitted even under
        # explicit opt-in (rotations cannot piggyback on a switched job).
        other = client.post(
            "/api/v1/connections",
            json={
                "name": "rotation-other-src",
                "kind": "source",
                "url": "https://rotation-other.example.com/api/v2",
                "token": "z",
            },
        ).json()
        params["source_id"] = other["id"]
        with pytest.raises(ValueError, match="resubmit"):
            verify_execution_pair(params)
        # An id switch with an unchanged fingerprint is honored under opt-in.
        fresh = store.pair_fingerprint(other["id"], tgt["id"])
        params.update(
            {
                "_snapshot_source_id": fresh["source_id"],
                "_snapshot_target_id": fresh["target_id"],
                "_snapshot_fp": fresh["fp"],
            }
        )
        verify_execution_pair(params)

    def test_iam_report_chains_after_pair_change(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        other_src = client.post(
            "/api/v1/connections",
            json={
                "name": "other-src",
                "kind": "source",
                "url": "https://other.example.com/api/v2",
                "token": "x",
            },
        ).json()
        other_tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "other-tgt",
                "kind": "target",
                "url": "https://other-t.example.com/api/v2",
                "token": "y",
            },
        ).json()
        client.post(
            "/api/v1/connections/active",
            json={"source_id": other_src["id"], "target_id": other_tgt["id"]},
        )
        # Connectionless iam-report must not demand an allow_pair_switch
        # flag its schema cannot supply.
        resp = client.post("/api/v1/iam/report", json={"job_id": first["job_id"]})
        assert resp.status_code == 202, resp.text

    def test_chained_dependency_reads_job_db(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        from pathlib import Path

        import aap_migration.cli.commands.export_import as ei_mod
        from aap_migration.api.context import open_state

        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        ref = get_job_manager().get_internal(first["job_id"])
        # Real export workers record progress in the job dir; materialize
        # the job DB the way a real export would.
        open_state(str(Path(ref["job_dir"]) / "migration_state.db"))
        seen: dict = {}

        def _spy(closure: Any, state: Any) -> Any:
            seen["url"] = state.database_url
            return []

        monkeypatch.setattr(ei_mod, "get_missing_dependencies", _spy)
        resp = client.post("/api/v1/imports/check-dependencies", json={"job_id": first["job_id"]})
        assert resp.status_code == 200, resp.text
        assert ref["job_dir"] in seen["url"]

    def test_sibling_artifacts_survive_delete(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        _fake_success(monkeypatch, "run_transform", artifact="xformed/orgs.json")
        second = _wait(
            client,
            client.post("/api/v1/transforms", json={"job_id": first["job_id"]}).json()["job_id"],
        )
        mgr = get_job_manager()
        assert (
            mgr.get_internal(first["job_id"])["job_dir"]
            == mgr.get_internal(second["job_id"])["job_dir"]
        )
        assert client.delete(f"/api/v1/jobs/{first['job_id']}").status_code == 200
        arts = client.get(f"/api/v1/jobs/{second['job_id']}/artifacts").json()
        assert "xformed/orgs.json" in arts["artifacts"]

    def test_retry_status_job_aware(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        from pathlib import Path

        from aap_migration.api.context import open_state

        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        ref = get_job_manager().get_internal(first["job_id"])
        open_state(str(Path(ref["job_dir"]) / "migration_state.db"))
        scoped = client.get(f"/api/v1/retry/status?job_id={first['job_id']}").json()
        assert "by_type" in scoped
        assert "warning" not in scoped
        assert client.get("/api/v1/retry/status?job_id=missing").status_code == 404

    def test_resume_accepts_cli_case(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        _fake_success(monkeypatch, "run_migrate_resume")
        resp = client.post("/api/v1/migrations/resume", json={"from_phase": "HOSTS"})
        assert resp.status_code == 202, resp.text
        job = _wait(client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")


class TestReviewHardening:
    """Coverage for review findings: auth fail-closed, strict branches, scrub,
    fence/backpressure, envelope truth, reset gates, source-only resolve,
    per-job console isolation, degraded startup, redirect policy."""

    def test_fail_closed_auth_and_throttle(self, client: TestClient, monkeypatch: Any) -> None:
        import aap_migration.api.security as sec

        sec._auth_failures.clear()
        try:
            # Fail closed without a token on any bind (the suite fixture
            # opts into ALLOW_ANON, so drop it first).
            monkeypatch.delenv("AAP_BRIDGE_ALLOW_ANON", raising=False)
            assert client.get("/api/v1/health").status_code == 401
            # An explicit non-loopback bind without token also fails closed.
            monkeypatch.setenv("AAP_BRIDGE_API_HOST", "0.0.0.0")
            assert client.get("/api/v1/health").status_code == 401
            # Explicit dev opt-in allows anonymous.
            monkeypatch.setenv("AAP_BRIDGE_ALLOW_ANON", "1")
            assert client.get("/api/v1/health").status_code == 200
            monkeypatch.delenv("AAP_BRIDGE_ALLOW_ANON")
            # Secondary rotation token accepted.
            monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "primary-1234567890")
            monkeypatch.setenv("AAP_BRIDGE_API_TOKEN_SECONDARY", "secondary-1234567890")
            good = {"X-API-Key": "secondary-1234567890"}
            assert client.get("/api/v1/health", headers=good).status_code == 200
            # Rapid failures trip per-IP throttling (429 past 20/60s).
            bad = {"X-API-Key": "wrong"}
            codes = {client.get("/api/v1/health", headers=bad).status_code for _ in range(25)}
            assert 429 in codes
        finally:
            sec._auth_failures.clear()
            monkeypatch.delenv("AAP_BRIDGE_API_HOST", raising=False)
            monkeypatch.delenv("AAP_BRIDGE_API_TOKEN", raising=False)
            monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)

    def test_strict_branches(self, client: TestClient) -> None:
        # Empty STARTUP_CWD (tmp dir, no state DB): lenient 200+warning,
        # strict=true 404 with string detail.
        for path, empty_key in [
            ("/api/v1/state/show", "warning"),
            ("/api/v1/state/mappings", "warning"),
            ("/api/v1/retry/status", "warning"),
            ("/api/v1/checkpoints", "warning"),
            ("/api/v1/checkpoints/resume-info", "warning"),
        ]:
            lenient = client.get(path)
            assert lenient.status_code == 200, path
            assert empty_key in lenient.json(), path
            strict = client.get(path + "?strict=true")
            assert strict.status_code == 404, path
            assert isinstance(strict.json()["detail"], str), path

    def test_console_scrub(self, client: TestClient, monkeypatch: Any) -> None:
        import aap_migration.api.services as services_mod
        from aap_migration.api.jobs import _scrub_output

        assert _scrub_output("Authorization: Bearer abcDEF123") == "Authorization: Bearer ***"
        assert "***" in _scrub_output('{"token": "s3cret-value"}')
        assert "***" in _scrub_output("X-API-Key: abc123 sent")
        assert "://***@" in _scrub_output("fetch https://user:pass@host/api")

        def _leaky(job: Any) -> Any:
            print("connecting with Bearer RAW-TOKEN-12345 and token=RAW-TOKEN-12345")
            return {"message": "ok"}

        monkeypatch.setattr(services_mod, "run_export", _leaky)

        assert (
            client.post(
                "/api/v1/connections",
                json={
                    "name": "s",
                    "kind": "source",
                    "url": "https://s.example.com",
                    "token": "t",
                },
            ).status_code
            == 201
        )
        # Minimal pair for submit gating.
        assert (
            client.post(
                "/api/v1/connections",
                json={
                    "name": "t",
                    "kind": "target",
                    "url": "https://t.example.com",
                    "token": "t",
                },
            ).status_code
            == 201
        )
        ids = {c["kind"]: c["id"] for c in client.get("/api/v1/connections").json()["items"]}
        client.post(
            "/api/v1/connections/active",
            json={"source_id": ids["source"], "target_id": ids["target"]},
        )
        job_id = client.post("/api/v1/exports", json={}).json()["job_id"]
        _wait(client, job_id)
        payload = client.get(f"/api/v1/jobs/{job_id}/console").json()
        assert payload["console_available"] is True
        console = payload["console"]
        assert "RAW-TOKEN-12345" not in console
        assert "***" in console

    def test_fence_and_backpressure(self, tmp_path: Any) -> None:
        import concurrent.futures

        from aap_migration.api.jobs import JobManager, QueueFullError

        # Shed-load via the public fence seam (note_timeout + submit): no
        # direct _orphans/_fenced_dirs planting, no private wait calls.
        mgr = JobManager(base_dir=str(tmp_path / "jobs"))
        never: concurrent.futures.Future = concurrent.futures.Future()
        fenced = str(tmp_path / "jobs" / "fenced")
        mgr._fences.note_timeout(future=never, pool=None, job_dir=fenced, pair_fp=None)
        try:
            mgr.submit("x", {}, lambda j: {}, job_dir=fenced)
            raise AssertionError("expected QueueFullError")
        except QueueFullError as exc:
            assert "fenced" in str(exc)
        # Orphan cap shedding via the same public seam.
        mgr2 = JobManager(base_dir=str(tmp_path / "jobs2"))
        cap_futures: list[concurrent.futures.Future[Any]] = [
            concurrent.futures.Future() for _ in range(60)
        ]
        for fut in cap_futures:
            mgr2._fences.note_timeout(future=fut, pool=None, job_dir="d", pair_fp=None)
        try:
            mgr2.submit("x", {}, lambda j: {})
            raise AssertionError("expected QueueFullError")
        except QueueFullError as exc:
            assert "timed-out" in str(exc) or "Too many" in str(exc)
        # Bounded unfence wait fails fast with a short job timeout, via the
        # public wrapper (never occupying the caller beyond the grace).
        mgr.job_timeout = 1
        assert mgr.wait_for_unfence(fenced) is False

    def test_envelope_total_truth(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        _fake_success(monkeypatch, "run_export")
        ids = [client.post("/api/v1/exports", json={}).json()["job_id"] for _ in range(3)]
        for job_id in ids:
            _wait(client, job_id)
        body = client.get("/api/v1/jobs?limit=2&offset=1").json()
        assert set(body) >= {"items", "total", "limit", "offset"}
        assert body["total"] == 3
        assert body["limit"] == 2 and body["offset"] == 1
        assert len(body["items"]) == 2

    def test_checkpoints_total_truth(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state
        from aap_migration.migration.checkpoint import CheckpointManager

        # Manager level (shared state object): true pre-page total plus
        # offset paging through the same ordering the endpoint serves.
        db = str(tmp_path / "checkpoints.db")
        state = open_state(db)
        mgr = CheckpointManager(state)
        for i in range(3):
            mgr.create_checkpoint(phase="export", progress_stats={}, description=f"c{i}")
        assert mgr.count_checkpoints(phase="export") == 3
        page = mgr.list_checkpoints(phase="export", limit=2, offset=1)
        assert len(page) == 2
        # Endpoint level: shared limit/offset/total keys with a true count.
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        body = client.get("/api/v1/checkpoints?limit=2&offset=1").json()
        assert set(body) >= {"checkpoints", "limit", "offset", "total"}
        assert isinstance(body["total"], int)

    def test_reset_gate_active_job(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        import threading

        import aap_migration.api.services as services_mod

        gate = threading.Event()

        def _slow(job: Any) -> Any:
            gate.wait(timeout=30)
            return {"message": "done"}

        monkeypatch.setattr(services_mod, "run_export", _slow)
        job_id = client.post("/api/v1/exports", json={}).json()["job_id"]
        deadline = __import__("time").time() + 15
        observed_running = False
        while __import__("time").time() < deadline:
            if client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "running":
                observed_running = True
                break
        # Fail loudly when the worker never picked the job up: without this,
        # the 409s below also pass for a merely queued job and the reset
        # gate's running-state coverage is unproven.
        assert observed_running, "worker never ran the blocking export"
        assert client.post("/api/v1/state/reset", json={"job_id": job_id}).status_code == 409
        # Server-default full reset also gated while a job runs.
        assert client.post("/api/v1/state/reset", json={}).status_code == 409
        gate.set()
        _wait(client, job_id)

    def test_source_only_ignores_target(self, client: TestClient) -> None:
        from aap_migration.api import store

        src = client.post(
            "/api/v1/connections",
            json={
                "name": "s",
                "kind": "source",
                "url": "https://s.example.com",
                "token": "t",
            },
        ).json()
        tgt = client.post(
            "/api/v1/connections",
            json={
                "name": "t",
                "kind": "target",
                "url": "https://t.example.com",
                "token": "t",
            },
        ).json()
        client.post(
            "/api/v1/connections/active", json={"source_id": src["id"], "target_id": tgt["id"]}
        )
        # Break the active target: source-only resolution must not care.
        store.delete_connection(tgt["id"])
        source, target = store.resolve_active_pair(src["id"], None, need="source")
        assert source["id"] == src["id"] and target is None

    def test_chained_console_isolation(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services as services_mod

        def _export(job: Any) -> Any:
            print("EXPORT-PHASE-MARKER")
            return {"message": "export ok"}

        def _transform(job: Any) -> Any:
            print("TRANSFORM-PHASE-MARKER")
            return {"message": "transform ok"}

        monkeypatch.setattr(services_mod, "run_export", _export)
        monkeypatch.setattr(services_mod, "run_transform", _transform)
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert first["status"] == "succeeded", first.get("error")
        second = _wait(
            client,
            client.post("/api/v1/transforms", json={"job_id": first["job_id"]}).json()["job_id"],
        )
        assert second["status"] == "succeeded", second.get("error")
        first_console = client.get(f"/api/v1/jobs/{first['job_id']}/console").json()["console"]
        assert "EXPORT-PHASE-MARKER" in first_console
        assert "TRANSFORM-PHASE-MARKER" not in first_console

    def test_degraded_startup_gates_submits(self, pair: Any, client: TestClient) -> None:
        from aap_migration.api.jobs import set_startup_degraded

        set_startup_degraded("test probe failure")
        try:
            assert client.post("/api/v1/exports", json={}).status_code == 503
            health = client.get("/api/v1/health")
            assert health.status_code == 503
            assert health.json()["status"] == "degraded"
        finally:
            set_startup_degraded(None)
        assert client.get("/api/v1/health").status_code == 200
        assert client.post("/api/v1/exports", json={}).status_code == 202

    def test_no_cross_origin_redirects(self) -> None:
        from aap_migration.client.base_client import BaseAPIClient

        client = BaseAPIClient(base_url="https://aap.example.com", token="t")
        assert client.client.follow_redirects is False

    def test_sync_reverify_fail_closed(self, client: TestClient) -> None:
        from aap_migration.utils.ssrf import reverify_execution_url_bounded

        try:
            reverify_execution_url_bounded("http://169.254.169.254/")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        # Unresolvable host fails closed at the sync test endpoint (400,
        # never a token-bearing fetch attempt).
        created = client.post(
            "/api/v1/connections",
            json={
                "name": "bad",
                "kind": "source",
                "url": "https://no-such-host.invalid/api",
                "token": "t",
            },
        )
        assert created.status_code == 201
        resp = client.post(f"/api/v1/connections/{created.json()['id']}/test")
        assert resp.status_code == 400

    def test_backpressure_variants_http(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Orphan-cap 429 and draining 503 surface over HTTP (not just queue depth)."""
        import concurrent.futures

        from api_shared import _fake_success

        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        mgr = get_job_manager()
        cap_futures: list[concurrent.futures.Future[Any]] = [
            concurrent.futures.Future() for _ in range(60)
        ]
        for idx, fut in enumerate(cap_futures):
            mgr._fences.note_timeout(future=fut, pool=None, job_dir=f"orphan-{idx}", pair_fp=None)
        try:
            resp = client.post("/api/v1/exports", json={})
            assert resp.status_code == 429, resp.text
            assert "timed-out" in resp.json()["detail"]
        finally:
            for fut in cap_futures:
                if not fut.done():
                    fut.set_result(None)
            mgr._fences.reap()
        # Draining sheds with 503 via the public shutdown_drain seam (the
        # per-test manager is discarded by the fixture, so no reset needed).
        mgr.shutdown_drain(timeout_secs=0)
        draining = client.post("/api/v1/exports", json={})
        assert draining.status_code == 503, draining.text
        assert "shutting down" in draining.json()["detail"]

    def test_fenced_pairs_shed_at_manager(self, tmp_path: Any) -> None:
        """Fenced dir and pair resubmissions fail fast at the manager gate."""
        import concurrent.futures

        from aap_migration.api.jobs import JobManager, QueueFullError

        mgr = JobManager(base_dir=str(tmp_path / "jobs"))
        never: concurrent.futures.Future = concurrent.futures.Future()
        fenced = str(tmp_path / "jobs" / "fenced")
        mgr._fences.note_timeout(future=never, pool=None, job_dir=fenced, pair_fp="pair-1")
        try:
            mgr.submit("x", {}, lambda j: {}, job_dir=fenced)
            raise AssertionError("expected QueueFullError for fenced dir")
        except QueueFullError as exc:
            assert "fenced" in str(exc)
        try:
            mgr.submit("x", {"_snapshot_fp": "pair-1"}, lambda j: {})
            raise AssertionError("expected QueueFullError for fenced pair")
        except QueueFullError as exc:
            assert "fenced" in str(exc)

    def test_filter_and_update_validation_branches(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        """Offset paging, kind-filter 400, update clashes, set_active kind checks."""
        from api_shared import _fake_success, _wait

        _fake_success(monkeypatch, "run_export")
        ids = [client.post("/api/v1/exports", json={}).json()["job_id"] for _ in range(3)]
        for job_id in ids:
            _wait(client, job_id)
        page = client.get("/api/v1/jobs?limit=2&offset=1").json()
        assert page["limit"] == 2 and page["offset"] == 1
        assert page["total"] == 3 and len(page["items"]) == 2
        # Invalid kind filter fails loudly at the schema layer (422).
        bad_kind = client.get("/api/v1/connections?kind=bogus")
        assert bad_kind.status_code == 422, bad_kind.text
        # Update validation: duplicate name clash and empty name.
        src, tgt = pair
        clash = client.put(
            f"/api/v1/connections/{src['id']}",
            json={
                "name": tgt["name"],
                "url": "https://src.example.com/api/v2",
                "token": "s3cret",
                "verify_ssl": False,
                "timeout": 30,
            },
        )
        assert clash.status_code == 400, clash.text
        empty = client.put(
            f"/api/v1/connections/{src['id']}",
            json={
                "name": "  ",
                "url": "https://src.example.com/api/v2",
                "token": "s3cret",
                "verify_ssl": False,
                "timeout": 30,
            },
        )
        assert empty.status_code == 400, empty.text
        # set_active with a wrong-kind id is a 400.
        wrong = client.post("/api/v1/connections/active", json={"source_id": tgt["id"]})
        assert wrong.status_code == 400, wrong.text
        # Unknown job status narrows loudly (500-class, never client 400).
        import pytest

        from aap_migration.api.jobs._records import InternalStatusError
        from aap_migration.api.routers._common import _narrow_status

        with pytest.raises(InternalStatusError, match="Unknown job status"):
            _narrow_status("nope")
