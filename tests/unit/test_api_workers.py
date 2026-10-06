"""Background worker unit tests (#5, #15, #16, #17).

Workers run through stubbed ``chained_ctx`` / ``call_command`` lifecycles
(never the shared checkout, never real CLIs): explicit-[] no-op scope,
cancel short-circuits, granular-import step defaults, IAM mutual
exclusion, single-path context resolution, fail-closed retry discovery,
and typed CLI error surfacing.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@contextmanager
def _stub_chained(
    workdir: Path, params: dict[str, Any], ctx: Any = None
) -> Iterator[tuple[Any, Any, Path, dict[str, Any]]]:
    """Yield a canned (ctx, config, workdir, params) worker lifecycle."""
    if ctx is None:
        ctx = SimpleNamespace(config=SimpleNamespace())
    config = SimpleNamespace(state=SimpleNamespace(db_path=""))
    yield ctx, config, workdir, params


def _patch_worker(
    monkeypatch: Any, module: Any, workdir: Path, params: dict[str, Any], ctx: Any = None
) -> dict[str, Any]:
    """Stub a worker module's lifecycle; return the call_command recorder."""
    recorded: dict[str, Any] = {"calls": []}

    @contextmanager
    def _chained(job: Any, **kwargs: Any) -> Iterator[Any]:
        with _stub_chained(workdir, params, ctx) as value:
            yield value

    def _recorder(cmd_name: str, _ctx: Any, **kwargs: Any) -> Any:
        recorded["calls"].append((cmd_name, kwargs))
        return None

    # Modules binding chained_ctx/call_command at import get module-level
    # stubs; function-level importers (iam) resolve through _core instead.
    import aap_migration.api.services._core as core

    for name, stub in (("chained_ctx", _chained), ("call_command", _recorder)):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, stub)
        else:
            monkeypatch.setattr(core, name, stub)
    return recorded


def _conn_ctx() -> Any:
    side = SimpleNamespace(
        url="https://src.example.com/api/v2",
        token="tok",
        verify_ssl=True,
        timeout=30,
    )
    return SimpleNamespace(config=SimpleNamespace(source=side, target=side))


class TestEtlWorkers:
    """P1 #5: ETL workers honor no-op scope, cancel, and step defaults."""

    def test_migrate_explicit_empty_is_noop_without_cli(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services.etl as etl

        recorded = _patch_worker(monkeypatch, etl, tmp_path, {"resource_types": []})
        from aap_migration.api.services._core import noop_result

        job: dict[str, Any] = {
            "job_id": "j1",
            "job_dir": str(tmp_path),
            "params": {"resource_types": []},
        }
        assert etl.run_migrate(job) == noop_result()  # type: ignore[arg-type]
        assert recorded["calls"] == []

    def test_migrate_cancel_before_start(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.etl as etl

        monkeypatch.setattr(etl, "_cancel_requested", lambda job: True)
        _patch_worker(monkeypatch, etl, tmp_path, {})
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        result = etl.run_migrate(job)  # type: ignore[arg-type]
        assert result["cancelled"] is True

    def test_granular_import_defaults_vs_explicit_empty(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services.etl as etl
        from aap_migration.migration.importers._registry import DEFAULT_GRANULAR_STEPS

        recorded = _patch_worker(monkeypatch, etl, tmp_path, {})
        (tmp_path / "xformed").mkdir()
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        result = etl.run_granular_import(job)  # type: ignore[arg-type]
        assert [c[1]["resource_type"] for c in recorded["calls"]] == [
            (step,) for step in DEFAULT_GRANULAR_STEPS
        ]
        assert result["steps_completed"] == list(DEFAULT_GRANULAR_STEPS)

        recorded2 = _patch_worker(monkeypatch, etl, tmp_path, {"steps": []})
        job2: dict[str, Any] = {
            "job_id": "j2",
            "job_dir": str(tmp_path),
            "params": {"steps": []},
        }
        result2 = etl.run_granular_import(job2)  # type: ignore[arg-type]
        assert recorded2["calls"] == []
        assert result2["steps_completed"] == []


class TestIamWorkers:
    """P1 #5 + P2 #16: IAM validation and single-path resolution."""

    def test_migrate_rejects_exclusive_flags_direct(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.iam as iam

        _patch_worker(
            monkeypatch,
            iam,
            tmp_path,
            {"skip_user_roles": True, "users_only": True},
            ctx=_conn_ctx(),
        )
        job: dict[str, Any] = {
            "job_id": "j1",
            "job_dir": str(tmp_path),
            "params": {"skip_user_roles": True, "users_only": True},
        }
        with pytest.raises(ValueError, match="mutually exclusive"):
            iam.run_iam_migrate(job)  # type: ignore[arg-type]

    def test_stored_from_ctx_resolves_single_path(self) -> None:
        from aap_migration.api.services.iam import _stored_from_ctx

        source = _stored_from_ctx(_conn_ctx(), "source")
        assert source["url"] == "https://src.example.com/api/v2"
        assert source["token"] == "tok"

    def test_stored_from_ctx_missing_side_is_value_error(self) -> None:
        from aap_migration.api.services.iam import _stored_from_ctx

        # P2 #16: typed ValueError, not AttributeError, when the context
        # carries no connection (no store fallback to mask it).
        with pytest.raises(ValueError, match="no source connection"):
            _stored_from_ctx(SimpleNamespace(config=SimpleNamespace()))


class TestReportingWorkers:
    """P1 #5: reporting workers validate scope before running."""

    def test_validate_skip_hosts_conflict(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.reporting as reporting

        _patch_worker(
            monkeypatch,
            reporting,
            tmp_path,
            {"skip_hosts": True, "resource_type": "hosts"},
        )
        job: dict[str, Any] = {
            "job_id": "j1",
            "job_dir": str(tmp_path),
            "params": {"skip_hosts": True, "resource_type": "hosts"},
        }
        with pytest.raises(ValueError, match="skip-hosts"):
            reporting.run_validate(job)  # type: ignore[arg-type]

    def test_analyze_requires_scope(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.reporting as reporting

        _patch_worker(monkeypatch, reporting, tmp_path, {})
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        with pytest.raises(ValueError, match="analyze_all"):
            reporting.run_analyze_dependencies(job)  # type: ignore[arg-type]


class TestRetryFailed:
    """P2 #17: state-DB discovery errors fail closed, never succeed empty."""

    def test_discovery_error_raises(self, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.context as ctx_mod
        import aap_migration.api.services.maintenance as maintenance

        class _BadState:
            database_url = "sqlite:////tmp/whatever.db"

            def get_failed_resource_types(self) -> list[str]:
                raise RuntimeError("db is locked")

        monkeypatch.setattr(ctx_mod, "open_default_state", lambda: _BadState())
        monkeypatch.setattr(ctx_mod, "write_job_config", lambda *a, **k: None)
        config = SimpleNamespace(state=SimpleNamespace(db_path=""))
        ctx = SimpleNamespace(config=config, _config=None)
        _patch_worker(monkeypatch, maintenance, tmp_path, {}, ctx=ctx)
        job: dict[str, Any] = {"job_id": "j1", "job_dir": str(tmp_path), "params": {}}
        with pytest.raises(ValueError, match="Could not list failed"):
            maintenance.run_retry_failed(job)  # type: ignore[arg-type]


class TestCallCommand:
    """P2 #15: CLI wiring drift surfaces the allowed-parameter list."""

    def test_unknown_param_names_allowed(self, tmp_path: Path) -> None:
        from aap_migration.api.services._core import call_command
        from aap_migration.cli.context import MigrationContext

        ctx = MigrationContext()
        with pytest.raises(TypeError) as exc_info:
            call_command("export", ctx, bogus_param=1)
        assert "bogus_param" in str(exc_info.value)
        assert "Allowed" in str(exc_info.value)

    def test_unknown_command_is_value_error(self, tmp_path: Path) -> None:
        from aap_migration.api.services._core import call_command
        from aap_migration.cli.context import MigrationContext

        with pytest.raises(ValueError, match="Unknown service command"):
            call_command("nope", MigrationContext())


class TestIamReport:
    """PR 142 #1 (P1): run_iam_report confinement + chaining branches.

    Uses the real ``workdir_ctx`` lifecycle with tmp job dirs (never the
    stubbed ``_patch_worker`` lifecycle); only the IAM report IO boundary
    (``load_audit_result_from_json`` / ``generate_iam_html_report``) is
    stubbed at the module boundary so the tests pin confinement/chaining
    without depending on real audit payloads.
    """

    def _stub_report_io(self, monkeypatch: Any) -> dict[str, Any]:
        import aap_migration.iam.report as report_mod

        recorded: dict[str, Any] = {"loads": []}
        sentinel = {"mode": "audit"}

        def _fake_load(path: str) -> Any:
            recorded["loads"].append(path)
            return sentinel

        def _fake_generate(result: Any) -> str:
            assert result is sentinel
            return "<html></html>"

        monkeypatch.setattr(report_mod, "load_audit_result_from_json", _fake_load)
        monkeypatch.setattr(report_mod, "generate_iam_html_report", _fake_generate)
        return recorded

    def test_traversal_rejected_generic(
        self, client: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services.iam as iam

        self._stub_report_io(monkeypatch)
        work = tmp_path / "work"
        work.mkdir()
        for candidate in ("../outside.json", "/etc/hostname"):
            job: dict[str, Any] = {
                "job_id": "r-traversal",
                "job_dir": str(work),
                "params": {"json_path": candidate},
            }
            with pytest.raises(
                ValueError, match="json_path must stay under the job file tree"
            ) as exc_info:
                iam.run_iam_report(job)  # type: ignore[arg-type]
            detail = str(exc_info.value)
            assert "/tmp" not in detail
            assert "/etc" not in detail
            assert "outside" not in detail

    def test_missing_json_path_raises(self, client: Any, tmp_path: Path, monkeypatch: Any) -> None:
        import aap_migration.api.services.iam as iam

        self._stub_report_io(monkeypatch)
        job: dict[str, Any] = {
            "job_id": "r-missing-arg",
            "job_dir": str(tmp_path),
            "params": {},
        }
        with pytest.raises(ValueError, match=r"json_path \(or job_id"):
            iam.run_iam_report(job)  # type: ignore[arg-type]

    def test_confined_missing_raises_not_found(
        self, client: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import aap_migration.api.services.iam as iam

        recorded = self._stub_report_io(monkeypatch)
        work = tmp_path / "work"
        work.mkdir()
        job: dict[str, Any] = {
            "job_id": "r-notfound",
            "job_dir": str(work),
            "params": {"json_path": "iam_reports/missing.json"},
        }
        with pytest.raises(ValueError, match="json report not found"):
            iam.run_iam_report(job)  # type: ignore[arg-type]
        assert recorded["loads"] == []

    def test_chained_relative_resolves_under_ref(
        self, client: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from pathlib import Path as _Path

        from api_shared import _wait

        import aap_migration.api.services.iam as iam
        from aap_migration.api.jobs import get_job_manager

        recorded = self._stub_report_io(monkeypatch)

        def _plant(job: Any) -> Any:
            base = _Path(job["job_dir"])
            target = base / "iam_reports" / "audit.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}")
            return {"json_report": "iam_reports/audit.json"}

        seed = get_job_manager().submit("seed", {}, _plant)
        done = _wait(client, seed["job_id"])
        assert done["status"] == "succeeded", done
        ref = get_job_manager().get_internal(seed["job_id"])

        work = tmp_path / "report-work"
        work.mkdir()
        job: dict[str, Any] = {
            "job_id": "r-chained",
            "job_dir": str(work),
            "params": {"job_id": seed["job_id"]},
        }
        result = iam.run_iam_report(job)  # type: ignore[arg-type]
        assert result["html_report"] == "iam_reports/audit.html"
        # Chained workdir is the referenced job's dir.
        assert (_Path(ref["job_dir"]) / result["html_report"]).is_file()
        assert recorded["loads"] == [
            str((_Path(ref["job_dir"]) / "iam_reports" / "audit.json").resolve())
        ]

    def test_chained_absolute_candidate(
        self, client: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from pathlib import Path as _Path

        from api_shared import _wait

        import aap_migration.api.services.iam as iam
        from aap_migration.api.jobs import get_job_manager

        recorded = self._stub_report_io(monkeypatch)

        def _plant_abs(job: Any) -> Any:
            base = _Path(job["job_dir"])
            target = base / "iam_reports" / "audit.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}")
            return {"json_report": str(target.resolve())}

        seed = get_job_manager().submit("seed-abs", {}, _plant_abs)
        done = _wait(client, seed["job_id"])
        assert done["status"] == "succeeded", done
        ref = get_job_manager().get_internal(seed["job_id"])

        work = tmp_path / "report-work-abs"
        work.mkdir()
        job: dict[str, Any] = {
            "job_id": "r-chained-abs",
            "job_dir": str(work),
            "params": {"job_id": seed["job_id"]},
        }
        result = iam.run_iam_report(job)  # type: ignore[arg-type]
        assert result["html_report"] == "iam_reports/audit.html"
        assert (_Path(ref["job_dir"]) / result["html_report"]).is_file()
        assert recorded["loads"] == [
            str((_Path(ref["job_dir"]) / "iam_reports" / "audit.json").resolve())
        ]
