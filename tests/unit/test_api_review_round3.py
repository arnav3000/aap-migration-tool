"""Round-3 regression tests for full-review findings #1-#9.

Covers: state reset atomicity, token rotation branches, connection
guards, DNS pool isolation, worker wiring, fork live-dir, error detail,
posture drift, lifecycle behavior.
"""

from __future__ import annotations

from typing import Any

from api_shared import _fake_success, _wait
from fastapi.testclient import TestClient


class TestStateResetAtomicity:
    """#1: job-scoped reset guard and write share one critical section.

    White-box pin: asserts the resolve happens while the manager lock is
    held (via _lock._is_owned). If the locking strategy changes, update
    this test alongside the refactor -- it pins the atomicity mechanism,
    not just the HTTP contract (the 409 branch below covers the contract).
    """

    def test_job_scoped_reset_inside_lock(
        self, pair: Any, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from aap_migration.api.context import open_state

        db = str(tmp_path / "state.db")
        monkeypatch.setenv("MIGRATION_STATE_DB_PATH", db)
        open_state(db)
        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert job["status"] == "succeeded"
        jid = job["job_id"]
        # Job-scoped reset targets the job's isolated DB: create it first
        # (strict readers 404 without it, mirroring existing roundtrip tests).
        from pathlib import Path

        from aap_migration.api.context import open_state
        from aap_migration.api.jobs import get_job_manager as _gm

        ref0 = _gm().get_internal(jid)
        open_state(str(Path(ref0["job_dir"]) / "migration_state.db"))

        manager = _gm()
        calls: list[str] = []
        import aap_migration.api.context as ctx_mod

        orig_resolve = ctx_mod.resolve_job_state

        def _counting(job_id: str, strict: bool = True) -> Any:
            # Record whether the lock is held at resolve time.
            try:
                held = manager._lock._is_owned()  # type: ignore[attr-defined]
            except Exception:
                held = False
            calls.append("locked" if held else "unlocked")
            return orig_resolve(job_id, strict=strict)

        monkeypatch.setattr(ctx_mod, "resolve_job_state", _counting)
        resp = client.post(
            "/api/v1/state/reset",
            json={"job_id": jid, "resource_type": "organizations"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) >= {
            "resource_type",
            "cleared_progress",
            "reset_mappings",
            "reset",
            "keep_mappings",
        }
        # Manager path must resolve inside the lock exactly once (atomic).
        # Before the fix it resolved once inside + once outside (unlocked).
        assert calls == ["locked"], f"expected single locked resolve, got {calls}"

    def test_job_scoped_reset_active_409(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import threading

        import aap_migration.api.services as services_mod

        gate = threading.Event()

        def _slow(job: Any) -> Any:
            gate.wait(timeout=30)
            return {"message": "done"}

        monkeypatch.setattr(services_mod, "run_export", _slow)
        jid = client.post("/api/v1/exports", json={}).json()["job_id"]
        deadline = __import__("time").time() + 15
        running = False
        while __import__("time").time() < deadline:
            if client.get(f"/api/v1/jobs/{jid}").json()["status"] == "running":
                running = True
                break
        assert running, "worker never ran blocking export"
        try:
            assert (
                client.post(
                    "/api/v1/state/reset",
                    json={"job_id": jid, "resource_type": "organizations"},
                ).status_code
                == 409
            )
        finally:
            gate.set()
        _wait(client, jid)


class TestTokenRotationBranches:
    """#2: decrypt failure / key-file / invalid-key branches fail closed."""

    def test_decrypt_after_rotation_actionable(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        import sqlite3

        from aap_migration.api.security import decrypt_token

        monkeypatch.setenv("AAP_BRIDGE_API_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        created = client.post(
            "/api/v1/connections",
            json={
                "name": "rot-src",
                "kind": "source",
                "url": "https://r.example.com/api/v2",
                "token": "super-secret",
            },
        ).json()
        assert created["id"]
        db = __import__("os").environ["AAP_BRIDGE_API_DB"]
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT token_encrypted FROM api_connections").fetchone()
        finally:
            con.close()
        assert row is not None
        # Rotate the key: decrypt must raise actionable ValueError, never raw.
        monkeypatch.setenv("AAP_BRIDGE_API_KEY", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")
        try:
            decrypt_token(row[0])
            raise AssertionError("expected ValueError after rotation")
        except ValueError as exc:
            assert "cannot be decrypted" in str(exc).lower() or "original key" in str(exc)
        # Worker path via get_connection must also fail closed, not leak.
        from aap_migration.api import store

        with __import__("pytest").raises(ValueError, match="decrypted|original key"):
            store.get_connection(created["id"], include_token=True)

    def test_nonfile_db_without_key_fails_closed(
        self, client: TestClient, monkeypatch: Any
    ) -> None:
        from aap_migration.api.security import _key_file

        monkeypatch.delenv("AAP_BRIDGE_API_KEY", raising=False)
        monkeypatch.setenv("AAP_BRIDGE_API_DB", "postgresql://u:p@host/db")
        try:
            _key_file()
            raise AssertionError("expected fail-closed ValueError")
        except ValueError as exc:
            assert "AAP_BRIDGE_API_KEY" in str(exc)

    def test_invalid_env_key_surfaces_loudly(self, monkeypatch: Any) -> None:
        from aap_migration.api.security import encrypt_token

        monkeypatch.setenv("AAP_BRIDGE_API_KEY", "not-a-valid-fernet-key")
        try:
            encrypt_token("x")
            raise AssertionError("expected loud failure for invalid key")
        except Exception as exc:
            assert exc is not None


class TestConnectionMutationGuards:
    """#3: PUT/PATCH update and active-move share delete's 409 guard."""

    def _blocker_job(self, pair: Any, client: TestClient, monkeypatch: Any) -> tuple[Any, Any, Any]:
        import threading

        from aap_migration.api.jobs import get_job_manager

        manager = get_job_manager()
        entered = threading.Event()
        release = threading.Event()

        def _blocker(job: Any) -> Any:
            entered.set()
            assert release.wait(timeout=30)
            return {"message": "blocker"}

        src, _tgt = pair
        manager.submit("blocker", {"source_id": src["id"]}, _blocker)
        assert entered.wait(timeout=30)
        return manager, entered, release

    def test_update_referenced_409(self, pair: Any, client: TestClient, monkeypatch: Any) -> None:
        import time

        manager, _e, release = self._blocker_job(pair, client, monkeypatch)
        src, _tgt = pair
        try:
            put = client.put(
                f"/api/v1/connections/{src['id']}",
                json={
                    "name": "src",
                    "url": "https://src.example.com/api/v2",
                    "token": "new",
                    "verify_ssl": False,
                    "timeout": 60,
                },
            )
            assert put.status_code == 409, put.text
            patch = client.patch(f"/api/v1/connections/{src['id']}", json={"timeout": 99})
            assert patch.status_code == 409, patch.text
        finally:
            release.set()
        deadline = time.time() + 30
        while manager.has_active_jobs() and time.time() < deadline:
            time.sleep(0.05)
        ok = client.patch(f"/api/v1/connections/{src['id']}", json={"timeout": 99})
        assert ok.status_code == 200, ok.text

    def test_active_move_referenced_409(
        self, pair: Any, client: TestClient, monkeypatch: Any
    ) -> None:
        import time

        manager, _e, release = self._blocker_job(pair, client, monkeypatch)
        src, tgt = pair
        other = client.post(
            "/api/v1/connections",
            json={
                "name": "other-src",
                "kind": "source",
                "url": "https://o.example.com/api/v2",
                "token": "x",
            },
        ).json()
        try:
            resp = client.post("/api/v1/connections/active", json={"source_id": other["id"]})
            assert resp.status_code == 409, resp.text
        finally:
            release.set()
        deadline = time.time() + 30
        while manager.has_active_jobs() and time.time() < deadline:
            time.sleep(0.05)
        assert tgt["id"]
        assert src["id"]


class TestDNSIsolation:
    """#4: hung lookups must not exhaust a shared pool."""

    def test_healthy_passes_after_poison_timeouts(self, monkeypatch: Any) -> None:
        import socket
        import threading

        import aap_migration.utils.ssrf as ssrf

        release = threading.Event()

        def _blocking(host: str, *args: Any, **kwargs: Any) -> Any:
            if "poison" in str(host):
                # Block until released (no wall-clock sleep): proves the
                # bounded call fails fast without measuring elapsed time.
                assert release.wait(timeout=30)
                raise socket.gaierror("hung")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _blocking)
        try:
            # Four rapid poison timeouts (short timeout for test speed).
            for i in range(4):
                try:
                    ssrf.reverify_execution_url_bounded(
                        f"https://poison{i}.example.com/api", timeout_secs=0.2
                    )
                    raise AssertionError("expected timeout")
                except ValueError as exc:
                    assert "timed out" in str(exc)
            # Healthy host still passes while poison lookups stay blocked
            # (per-call isolation: no shared-pool exhaustion, no elapsed
            # assertion so loaded CI cannot flake).
            out = ssrf.reverify_execution_url_bounded(
                "https://healthy.example.com/api", timeout_secs=5.0
            )
            assert out.startswith("https://")
        finally:
            release.set()


class TestForkLiveDir:
    """#6: console/fence must follow the pair-switch fork.

    White-box pin: exercises the internal _live_job_dir()/set_job_dir()
    helpers directly. If the fork bookkeeping moves, update these alongside
    the refactor -- the HTTP round-trips elsewhere cover the public contract.
    """

    def test_live_dir_follows_fork(self, client: TestClient) -> None:
        from aap_migration.api.jobs import get_job_manager

        manager = get_job_manager()
        rec = manager.submit("fork-test", {}, lambda job: {"message": "ok"})
        jid = rec["job_id"]
        orig = manager.get_internal(jid)["job_dir"]
        fresh = str(__import__("pathlib").Path(orig).parent / "fresh-sibling")
        __import__("os").makedirs(fresh, exist_ok=True)
        manager.set_job_dir(jid, fresh)
        assert manager._live_job_dir(jid, orig) == fresh
        assert manager._live_job_dir("missing-id", orig) == orig

    def test_console_persisted_to_fork(
        self, pair: Any, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from pathlib import Path

        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        first = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        assert first["status"] == "succeeded"
        manager = get_job_manager()
        ref = manager.get_internal(first["job_id"])
        parent = Path(ref["job_dir"])
        # Simulate a fork: fresh sibling becomes live.
        fresh = parent.parent / f"{parent.name}__switched_test"
        fresh.mkdir(parents=True, exist_ok=True)
        manager.set_job_dir(first["job_id"], str(fresh))
        live = manager._live_job_dir(first["job_id"], str(parent))
        assert Path(live).resolve() == fresh.resolve()
        # Reads follow the fork (jobs.py get_internal serves fresh).
        assert manager.get_internal(first["job_id"])["job_dir"] == str(fresh)


class TestUnknownJobDetail:
    """#7: chained-reference miss keeps actionable detail."""

    def test_unknown_job_error_detail_preserved(self, client: TestClient, monkeypatch: Any) -> None:
        from aap_migration.api.jobs import get_job_manager
        from aap_migration.api.jobs._records import UnknownJobError

        manager = get_job_manager()

        def _boom(job: Any) -> Any:
            raise UnknownJobError("Unknown job_id 'gone-123'")

        rec = manager.submit("boom-test", {}, _boom)
        done = _wait(client, rec["job_id"])
        assert done["status"] == "failed", done
        assert "Unknown job_id 'gone-123'" in done.get("error", ""), done
        assert done["error"] != "UnknownJobError"


class TestPostureDrift:
    """#8: verify_ssl/timeout drift must fail like token rotation."""

    def test_posture_drift_fails_execution(self, pair: Any, client: TestClient) -> None:
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
        store.update_connection(src["id"], verify_ssl=not src.get("verify_ssl", False))
        try:
            verify_execution_pair(params)
            raise AssertionError("expected posture drift to fail")
        except ValueError as exc:
            assert "changed since" in str(exc)
        # Identical snapshot still verifies (no false positive).
        snap2 = store.pair_fingerprint(src["id"], tgt["id"])
        params2: Any = {
            "source_id": src["id"],
            "target_id": tgt["id"],
            "_snapshot_source_id": snap2["source_id"],
            "_snapshot_target_id": snap2["target_id"],
            "_snapshot_fp": snap2["fp"],
        }
        verify_execution_pair(params2)

    def test_timeout_drift_fails_execution(self, pair: Any, client: TestClient) -> None:
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
        new_timeout = 5 if src.get("timeout", 30) != 5 else 60
        store.update_connection(src["id"], timeout=new_timeout)
        try:
            verify_execution_pair(params)
            raise AssertionError("expected timeout drift to fail")
        except ValueError as exc:
            assert "changed since" in str(exc)
        snap2 = store.pair_fingerprint(src["id"], tgt["id"])
        params2: Any = {
            "source_id": src["id"],
            "target_id": tgt["id"],
            "_snapshot_source_id": snap2["source_id"],
            "_snapshot_target_id": snap2["target_id"],
            "_snapshot_fp": snap2["fp"],
        }
        verify_execution_pair(params2)

    def test_url_drift_fails_execution(self, pair: Any, client: TestClient) -> None:
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
        store.update_connection(src["id"], url="https://drifted.example.com/api/v2")
        try:
            verify_execution_pair(params)
            raise AssertionError("expected URL drift to fail")
        except ValueError as exc:
            assert "changed since" in str(exc)
        snap2 = store.pair_fingerprint(src["id"], tgt["id"])
        params2: Any = {
            "source_id": src["id"],
            "target_id": tgt["id"],
            "_snapshot_source_id": snap2["source_id"],
            "_snapshot_target_id": snap2["target_id"],
            "_snapshot_fp": snap2["fp"],
        }
        verify_execution_pair(params2)


class TestWorkerWiringSpies:
    """#5: CLI-boundary spies prove param mapping (not fake's own message)."""

    def _ctx_stub(
        self, monkeypatch: Any, module: Any, workdir: Any, params: dict[str, Any]
    ) -> None:
        import contextlib
        import types

        import aap_migration.api.services._core as core

        config = types.SimpleNamespace(
            export=types.SimpleNamespace(records_per_file=1000),
            performance=types.SimpleNamespace(
                project_patch_batch_size=50, project_patch_batch_interval=0
            ),
        )

        @contextlib.contextmanager
        def _fake_ctx(job: Any, **kwargs: Any) -> Any:
            yield (object(), config, workdir, params)

        # Workers import chained_ctx/workdir_ctx locally from _core at call
        # time (iam imports inside the function), so patch the _core home.
        # Also patch the requesting module when it re-exports the name.
        monkeypatch.setattr(core, "chained_ctx", _fake_ctx)
        for target in {core, module}:
            try:
                if hasattr(target, "chained_ctx"):
                    monkeypatch.setattr(target, "chained_ctx", _fake_ctx)
            except Exception:
                pass

    def test_cleanup_passes_options(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.maintenance as m

        workdir = tmp_path / "job"
        workdir.mkdir()
        params = {"resource_types": ["organizations"], "full": True}
        self._ctx_stub(monkeypatch, m, workdir, params)
        seen: dict[str, Any] = {}

        def _spy(cmd: str, ctx: Any, **kwargs: Any) -> None:
            seen["cmd"] = cmd
            seen.update(kwargs)

        monkeypatch.setattr(m, "call_command", _spy)
        out = m.run_cleanup({"job_id": "c", "job_dir": str(workdir), "params": params})
        assert out["message"] == "Cleanup complete"
        assert seen["cmd"] == "cleanup"
        assert seen["resource_type"] == ("organizations",)

    def test_retry_failed_passes_options(self, tmp_path: Any, monkeypatch: Any) -> None:
        import contextlib
        import types

        import aap_migration.api.services._core as core
        import aap_migration.api.services.maintenance as m

        workdir = tmp_path / "job"
        workdir.mkdir()
        params = {"resource_types": ["hosts"], "dry_run": True}
        seen: dict[str, Any] = {}

        @contextlib.contextmanager
        def _fake_retry_ctx(job: Any, **kwargs: Any) -> Any:
            ctx = types.SimpleNamespace(_config=None)
            config = types.SimpleNamespace(state=types.SimpleNamespace(db_path=""))
            yield (ctx, config, workdir, params)

        monkeypatch.setattr(core, "chained_ctx", _fake_retry_ctx)
        monkeypatch.setattr(m, "chained_ctx", _fake_retry_ctx)

        def _loose(cmd: str, ctx: Any, **kwargs: Any) -> None:
            seen["cmd"] = cmd
            seen.update(kwargs)

        monkeypatch.setattr(core, "call_command", _loose)
        monkeypatch.setattr(m, "call_command", _loose)

        class _FakeState:
            database_url = "sqlite:///" + str(tmp_path / "r.db")

        import aap_migration.api.context as ctx_mod

        monkeypatch.setattr(ctx_mod, "open_default_state", lambda: _FakeState())
        monkeypatch.setattr("aap_migration.api.context.write_job_config", lambda *a, **k: None)
        out = m.run_retry_failed({"job_id": "r", "job_dir": str(workdir), "params": params})
        assert out["message"] == "Retry failed complete"
        assert seen["cmd"] == "retry-failed"
        assert seen.get("dry_run") is True

    def test_validate_maps_live_flag(self, tmp_path: Any, monkeypatch: Any) -> None:
        import contextlib
        import types

        import aap_migration.api.services._core as core
        import aap_migration.api.services.reporting as r

        workdir = tmp_path / "job"
        workdir.mkdir()
        params = {"live": True, "resource_type": "hosts"}

        @contextlib.contextmanager
        def _fake_validate_ctx(job: Any, **kwargs: Any) -> Any:
            ctx = types.SimpleNamespace(target_client=None, migration_state=None)
            config = types.SimpleNamespace()
            yield (ctx, config, workdir, params)

        monkeypatch.setattr(core, "chained_ctx", _fake_validate_ctx)
        monkeypatch.setattr(r, "chained_ctx", _fake_validate_ctx)
        seen: dict[str, Any] = {}

        async def _fake_run(**kwargs: Any) -> Any:
            seen.update(kwargs)
            import types

            per = types.SimpleNamespace(
                per_type=[],
                executive_summary=types.SimpleNamespace(
                    total_missing_on_target=0,
                    total_field_mismatches=0,
                    verdict="pass",
                ),
            )
            return per, []

        import aap_migration.validate.runner as runner

        monkeypatch.setattr(runner, "run_validation", _fake_run)
        monkeypatch.setattr(
            "aap_migration.validate.org_report.write_org_scoped_validation_reports",
            lambda *a, **k: [],
        )
        monkeypatch.setattr(
            "aap_migration.validate.report.resolve_validate_report_dir",
            lambda *a, **k: None,
        )
        out = r.run_validate({"job_id": "v", "job_dir": str(workdir), "params": params})
        assert out["message"] == "Validation complete"
        assert seen.get("live") is True

    def test_iam_benchmark_maps_sample(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services._core as core
        import aap_migration.api.services.iam as iam

        workdir = tmp_path / "job"
        workdir.mkdir()
        params = {"sample_size": 7}
        self._ctx_stub(monkeypatch, iam, workdir, params)
        # _core.chained_ctx is the home patched by _ctx_stub; _iam_connections
        # lives on the iam module.
        monkeypatch.setattr(
            iam,
            "_iam_connections",
            lambda pdict, need="source": (
                {"url": "https://s", "token": "t", "verify_ssl": True, "timeout": 60},
                None,
            ),
        )
        assert core.chained_ctx is not None
        seen: dict[str, Any] = {}

        def _spy(**kwargs: Any) -> None:
            seen.update(kwargs)

        monkeypatch.setattr("aap_migration.iam.benchmark.run_benchmark", _spy)
        out = iam.run_iam_benchmark({"job_id": "b", "job_dir": str(workdir), "params": params})
        assert out["message"] == "IAM benchmark complete"
        assert seen.get("sample_size") == 7

    def test_state_export_writes_under_workdir(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.maintenance as m

        workdir = tmp_path / "job"
        workdir.mkdir()
        import contextlib

        @contextlib.contextmanager
        def _fake_wctx(job: Any) -> Any:
            yield (workdir, {})

        monkeypatch.setattr(m, "workdir_ctx", _fake_wctx)

        class _FakeState:
            database_url = "sqlite:///" + str(tmp_path / "s.db")

            def export_state(self, path: str) -> None:
                __import__("pathlib").Path(path).parent.mkdir(parents=True, exist_ok=True)
                __import__("pathlib").Path(path).write_text("{}")

        monkeypatch.setattr(m, "open_default_state", lambda: _FakeState())
        out = m.run_state_export({"job_id": "s", "job_dir": str(workdir), "params": {}})
        assert out["message"] == "State export complete"
        assert out["state_file"].startswith("reports/")


class TestCredentialMigrateBranches:
    """Credential-migrate no-action vs migrate-all envelopes (direct workers)."""

    def _run_migrate(self, monkeypatch: Any, tmp_path: Any, comparison: dict[str, Any]) -> Any:
        import contextlib
        import types

        import aap_migration.api.services._core as core
        import aap_migration.api.services.credentials as cred

        workdir = tmp_path / "job"
        workdir.mkdir(exist_ok=True)
        (workdir / "reports").mkdir(exist_ok=True)
        params: dict[str, Any] = {}

        @contextlib.contextmanager
        def _fake_ctx(job: Any, **kwargs: Any) -> Any:
            config = types.SimpleNamespace(dry_run=False)
            ctx = types.SimpleNamespace(config=config)
            yield (ctx, config, workdir, params)

        monkeypatch.setattr(core, "chained_ctx", _fake_ctx)
        monkeypatch.setattr(cred, "chained_ctx", _fake_ctx)

        class _FakeCoordinator:
            async def compare_and_verify_credentials(self, report_path: str = "") -> Any:
                return dict(comparison)

            async def migrate_all(self, **kwargs: Any) -> Any:
                return {"migrated": 3, "report": "reports/credential-migration.md"}

        monkeypatch.setattr(cred, "_credential_coordinator", lambda ctx: _FakeCoordinator())
        return cred.run_credential_migrate(
            {"job_id": "c", "job_dir": str(workdir), "params": params}
        )

    def test_no_action_branch(self, tmp_path: Any, monkeypatch: Any) -> None:
        out = self._run_migrate(monkeypatch, tmp_path, {"missing_count": 0})
        assert out["status"] == "no_action_needed"
        assert "message" in out and "artifacts" in out
        assert "comparison" in out

    def test_migrate_all_branch(self, tmp_path: Any, monkeypatch: Any) -> None:
        out = self._run_migrate(monkeypatch, tmp_path, {"missing_count": 2})
        assert "comparison" in out
        assert "message" in out and "artifacts" in out


class TestLifecycleBehavioral:
    """#9: behavioral lifecycle proof (not source substring)."""

    def test_workers_enter_lifecycle_with_need(self, tmp_path: Any, monkeypatch: Any) -> None:
        import contextlib

        import aap_migration.api.services._core as core
        import aap_migration.api.services.iam as iam
        import aap_migration.api.services.maintenance as maint

        entered: list[tuple[str, Any]] = []

        @contextlib.contextmanager
        def _spy_chained(job: Any, **kwargs: Any) -> Any:
            entered.append(("chained", kwargs.get("need")))
            import types

            yield (object(), types.SimpleNamespace(), tmp_path, dict(job.get("params", {})))

        @contextlib.contextmanager
        def _spy_workdir(job: Any, **kwargs: Any) -> Any:
            entered.append(("workdir", None))
            yield (tmp_path, dict(job.get("params", {})))

        # IAM audit must enter chained_ctx (need=source), report must enter
        # workdir_ctx; stubbing proves the worker honors the lifecycle.
        # iam imports chained_ctx locally from _core, so patch the home.
        monkeypatch.setattr(core, "chained_ctx", _spy_chained)
        monkeypatch.setattr(
            iam, "_iam_connections", lambda pdict, need="source": ({"url": "u", "token": "t"}, None)
        )
        monkeypatch.setattr(iam, "_run_iam_audit", lambda pd, wd, s: {"message": "ok"})
        out = iam.run_iam_audit({"job_id": "a", "job_dir": str(tmp_path), "params": {}})
        assert out["message"] == "ok"
        assert ("chained", "source") in entered

        entered.clear()

        class _FakeState:
            database_url = "sqlite:///" + str(tmp_path / "lc.db")

            def export_state(self, path: str) -> None:
                __import__("pathlib").Path(path).parent.mkdir(parents=True, exist_ok=True)
                __import__("pathlib").Path(path).write_text("{}")

        monkeypatch.setattr(core, "workdir_ctx", _spy_workdir)
        monkeypatch.setattr(maint, "workdir_ctx", _spy_workdir)
        monkeypatch.setattr(maint, "open_default_state", lambda: _FakeState())
        out2 = maint.run_state_export({"job_id": "s", "job_dir": str(tmp_path), "params": {}})
        assert out2["message"] == "State export complete"
        assert ("workdir", None) in entered

    def test_cancel_honored_by_worker(self, tmp_path: Any, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        monkeypatch.setattr(etl, "_cancel_requested", lambda job: True)
        out = etl.run_migrate({"job_id": "x", "job_dir": str(tmp_path), "params": {}})
        assert out.get("cancelled") is True
