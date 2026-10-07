"""Lifecycle proof for the REST API: worker passthrough, cancel, startup probes.

Covers review findings #2 (CLI parity unproven by mocked round-trips),
#20 (worker cancel branches never exercised), and #13 (lifespan probe
branches untested). Spies assert the real workers invoke the CLI layer
with the caller's options instead of asserting a mock's own message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI


class _CallLog:
    """Thread-safe recorder for call_command-style spies."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return None


class TestWorkerPassthrough:
    """Real workers run with a stubbed context; only the CLI boundary is
    stubbed (proves #2: workers map params to CLI options faithfully)."""

    def _ctx_stub(
        self, monkeypatch: Any, module: Any, workdir: Path, params: dict[str, Any]
    ) -> None:
        import contextlib
        import types

        config = types.SimpleNamespace(
            export=types.SimpleNamespace(records_per_file=1000),
            performance=types.SimpleNamespace(
                project_patch_batch_size=50, project_patch_batch_interval=0
            ),
        )

        @contextlib.contextmanager
        def _fake_ctx(job: Any) -> Any:
            yield (object(), config, workdir, params)

        monkeypatch.setattr(module, "chained_ctx", _fake_ctx)
        monkeypatch.setattr(module, "_cancel_requested", lambda job: False)

    def test_export_passes_cli_options(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        workdir = tmp_path / "job"
        workdir.mkdir()
        params = {"resource_types": ["organizations"], "resume": False}
        self._ctx_stub(monkeypatch, etl, workdir, params)
        log = _CallLog()
        monkeypatch.setattr(etl, "call_command", log)
        result = etl.run_export({"job_id": "e", "job_dir": str(workdir), "params": params})
        assert result["message"] == "Export complete"
        assert log.calls, "run_export never invoked the CLI layer"
        assert log.calls[0][0] == "export"
        assert log.kwargs[0].get("resource_type") == ("organizations",)
        assert str(log.kwargs[0].get("output", "")).startswith(str(workdir))

    def test_import_passes_dry_run(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        workdir = tmp_path / "job"
        workdir.mkdir()
        params: dict[str, Any] = {"dry_run": True, "phase": "all"}
        self._ctx_stub(monkeypatch, etl, workdir, params)
        log = _CallLog()
        monkeypatch.setattr(etl, "call_command", log)
        result = etl.run_import({"job_id": "i", "job_dir": str(workdir), "params": params})
        assert result["message"] == "Import complete"
        assert log.calls and log.calls[0][0] == "import"
        assert log.kwargs[0].get("dry_run") is True
        assert str(log.kwargs[0].get("input_dir", "")).startswith(str(workdir))

    def test_migrate_runs_inside_job_workdir(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Full migrate threads the isolated job workdir (guards #8)."""
        import aap_migration.api.services.etl as etl
        import aap_migration.cli.commands.migrate as migrate_mod

        workdir = tmp_path / "job"
        workdir.mkdir()
        params: dict[str, Any] = {"skip_prep": True, "phase": "all"}
        self._ctx_stub(monkeypatch, etl, workdir, params)
        seen: dict[str, Any] = {}

        def _spy(*args: Any, **kwargs: Any) -> None:
            seen["kwargs"] = kwargs

        monkeypatch.setattr(migrate_mod, "_run_migration_workflow", _spy)
        result = etl.run_migrate({"job_id": "m", "job_dir": str(workdir), "params": params})
        assert result["message"] == "Migration workflow complete"
        base_dir = seen.get("kwargs", {}).get("base_dir")
        assert base_dir is not None, "run_migrate did not pass base_dir"
        assert Path(str(base_dir)).resolve() == workdir.resolve()

    def test_granular_empty_steps_is_noop(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Explicit [] selects none (consistent with resource_types, #7)."""
        import aap_migration.api.services.etl as etl

        workdir = tmp_path / "job"
        workdir.mkdir()
        self._ctx_stub(monkeypatch, etl, workdir, {"steps": []})
        log = _CallLog()
        monkeypatch.setattr(etl, "call_command", log)
        result = etl.run_granular_import(
            {"job_id": "g", "job_dir": str(workdir), "params": {"steps": []}}
        )
        assert log.calls == [], f"explicit [] must run zero steps, ran {len(log.calls)}"
        assert result.get("steps_completed") == []


class TestWorkerCancel:
    """Cancel lifecycle branches at the worker level (#20)."""

    def test_migrate_cancel_before_start(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        monkeypatch.setattr(etl, "_cancel_requested", lambda job: True)
        job: Any = {"job_id": "x", "job_dir": str(tmp_path), "params": {}}
        result = etl.run_migrate(job)
        assert result.get("cancelled") is True

    def test_granular_cancel_mid_loop(self, tmp_path: Path, monkeypatch: Any) -> None:
        import contextlib

        import aap_migration.api.services.etl as etl

        log = _CallLog()
        monkeypatch.setattr(etl, "call_command", log)
        states = iter([False, True])

        def _flag(job: Any) -> bool:
            try:
                return next(states)
            except StopIteration:
                return True

        monkeypatch.setattr(etl, "_cancel_requested", _flag)

        @contextlib.contextmanager
        def _fake_ctx(job: Any) -> Any:
            params = {"steps": ["organizations", "users"], "dry_run": False}
            yield (object(), object(), tmp_path, params)

        monkeypatch.setattr(etl, "chained_ctx", _fake_ctx)
        job: Any = {"job_id": "g", "job_dir": str(tmp_path), "params": {}}
        result = etl.run_granular_import(job)
        assert result.get("cancelled") is True
        assert result.get("steps_completed") == ["organizations"]
        assert len(log.calls) == 1

    def test_cancel_flag_read_failure_warns(self, caplog: Any, monkeypatch: Any) -> None:
        import aap_migration.api.jobs as jobs_mod
        from aap_migration.api.services._core import _cancel_requested

        def _boom() -> Any:
            raise RuntimeError("db gone")

        monkeypatch.setattr(jobs_mod, "get_job_manager", _boom)
        with caplog.at_level("WARNING", logger="aap_migration.api.services"):
            assert _cancel_requested({"job_id": "z"}) is False
        assert "cancel-flag read failed" in caplog.text


class TestLifespanProbes:
    """Startup/shutdown probe branches (#13)."""

    async def test_db_failure_marks_degraded(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.app as app_mod
        from aap_migration.api.app import lifespan
        from aap_migration.api.jobs import set_startup_degraded, startup_degraded_reason

        set_startup_degraded(None)
        monkeypatch.setattr(
            app_mod, "init_api_db", lambda: (_ for _ in ()).throw(RuntimeError("disk gone"))
        )
        monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", str(tmp_path / "jobs"))
        monkeypatch.delenv("AAP_BRIDGE_ALLOW_DEGRADED_STARTUP", raising=False)
        async with lifespan(FastAPI()):
            reason = startup_degraded_reason()
            assert reason is not None and "api-db" in reason
        set_startup_degraded(None)

    async def test_degraded_opt_in_warns_only(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.app as app_mod
        from aap_migration.api.app import lifespan
        from aap_migration.api.jobs import set_startup_degraded, startup_degraded_reason

        set_startup_degraded(None)
        monkeypatch.setattr(
            app_mod, "init_api_db", lambda: (_ for _ in ()).throw(RuntimeError("disk gone"))
        )
        monkeypatch.setenv("AAP_BRIDGE_ALLOW_DEGRADED_STARTUP", "1")
        monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", str(tmp_path / "jobs"))
        async with lifespan(FastAPI()):
            assert startup_degraded_reason() is None
        set_startup_degraded(None)

    async def test_unwritable_job_dir_marks_degraded(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import aap_migration.api.app as app_mod
        from aap_migration.api.app import lifespan
        from aap_migration.api.jobs import set_startup_degraded, startup_degraded_reason

        set_startup_degraded(None)
        monkeypatch.setattr(app_mod, "init_api_db", lambda: str(tmp_path / "api.db"))
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir")
        monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", str(blocker))
        monkeypatch.delenv("AAP_BRIDGE_ALLOW_DEGRADED_STARTUP", raising=False)
        async with lifespan(FastAPI()):
            reason = startup_degraded_reason()
            assert reason is not None and "job-dir" in reason
        set_startup_degraded(None)

    async def test_shutdown_drain_leftovers_warn(
        self, tmp_path: Path, monkeypatch: Any, caplog: Any
    ) -> None:
        import aap_migration.api.app as app_mod
        import aap_migration.api.jobs as jobs_mod
        from aap_migration.api.app import lifespan
        from aap_migration.api.jobs import set_startup_degraded

        set_startup_degraded(None)
        monkeypatch.setattr(app_mod, "init_api_db", lambda: str(tmp_path / "api.db"))
        monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", str(tmp_path / "jobs"))

        class _StubManager:
            def shutdown_drain(self) -> dict[str, int]:
                return {"pending": 2, "running": 1}

        monkeypatch.setattr(jobs_mod, "manager_or_none", lambda: _StubManager())
        with caplog.at_level("WARNING", logger="aap_migration.api.app"):
            async with lifespan(FastAPI()):
                pass
        assert "queued" in caplog.text or "running" in caplog.text
        set_startup_degraded(None)

    async def test_shutdown_no_manager_warns(
        self, tmp_path: Path, monkeypatch: Any, caplog: Any
    ) -> None:
        import aap_migration.api.app as app_mod
        import aap_migration.api.jobs as jobs_mod
        from aap_migration.api.app import lifespan
        from aap_migration.api.jobs import set_startup_degraded

        set_startup_degraded(None)
        monkeypatch.setattr(app_mod, "init_api_db", lambda: str(tmp_path / "api.db"))
        monkeypatch.setattr(jobs_mod, "manager_or_none", lambda: None)
        monkeypatch.setenv("AAP_BRIDGE_JOB_DIR", str(tmp_path / "jobs"))
        with caplog.at_level("WARNING", logger="aap_migration.api.app"):
            async with lifespan(FastAPI()):
                pass
        assert "no job manager" in caplog.text
        set_startup_degraded(None)
