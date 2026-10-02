"""Regression tests for code-review round 2 (PR #127 follow-up).

Covers findings #2 (job-scoped import scope), #4 (fork persistence),
#5/#10 (guard single-home), #7 (shared worker lifecycle), and #11
(schema inheritance). Finding #1 (importer factory) is covered by
``test_importer_factory.py``.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient


class TestGuardSingleHome:
    """#5/#10: router guards delegate to the manager read model."""

    def test_reset_unknown_job_id_404(self, client: TestClient) -> None:
        resp = client.post("/api/v1/state/reset", json={"job_id": "does-not-exist"})
        assert resp.status_code == 404, resp.text

    def test_import_unknown_job_id_404(self, client: TestClient) -> None:
        # The referenced-job lookup runs before path confinement, so an
        # unknown job_id 404s regardless of the state_file value.
        resp = client.post(
            "/api/v1/state/import",
            json={"state_file": "backup.json", "job_id": "does-not-exist"},
        )
        assert resp.status_code == 404, resp.text

    def test_manager_guard_message_parity(self) -> None:
        from aap_migration.api.jobs._reads import _ACTIVE_JOBS_MESSAGE, _active_count

        assert "{active}" in _ACTIVE_JOBS_MESSAGE
        assert _active_count([{"status": "running"}, {"status": "succeeded"}]) == 1


class TestForkPersistence:
    """#4: pair-switch forks are persisted to the job record."""

    def test_set_job_dir_persists(
        self, pair: Any, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from api_shared import _fake_success, _wait

        from aap_migration.api.jobs import get_job_manager

        _fake_success(monkeypatch, "run_export")
        job = _wait(client, client.post("/api/v1/exports", json={}).json()["job_id"])
        manager = get_job_manager()
        fork = str(tmp_path / "forked-dir")
        manager.set_job_dir(job["job_id"], fork)
        assert manager.get_internal(job["job_id"])["job_dir"] == fork

    def test_chained_ctx_persists_fork(
        self, client: TestClient, monkeypatch: Any, tmp_path: Any
    ) -> None:
        from pathlib import Path

        import aap_migration.api.services._core as core
        from aap_migration.api.jobs import get_job_manager

        manager = get_job_manager()
        rec = manager.submit("probe", {}, lambda j: {"message": "ok"})
        jid = rec["job_id"]
        parent = Path(manager.get_internal(jid)["job_dir"])
        fresh = parent.parent / (parent.name + "__switched_abc123")
        fresh.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            pass

        monkeypatch.setattr(core, "setup_chained", lambda *a, **k: (_Ctx(), object(), fresh))
        with core.chained_ctx(manager.get_internal(jid), need="none") as (
            _ctx,
            _cfg,
            workdir,
            _params,
        ):
            assert Path(workdir) == fresh
        assert manager.get_internal(jid)["job_dir"] == str(fresh)


class TestSharedWorkerLifecycle:
    """#7: every IAM/retry/state worker goes through the shared lifecycle."""

    @pytest.mark.parametrize(
        "module_name, worker, lifecycle",
        [
            ("aap_migration.api.services.iam", "run_iam_audit", "chained_ctx"),
            ("aap_migration.api.services.iam", "run_iam_migrate", "chained_ctx"),
            ("aap_migration.api.services.iam", "run_iam_benchmark", "chained_ctx"),
            ("aap_migration.api.services.iam", "run_iam_report", "workdir_ctx"),
            ("aap_migration.api.services.maintenance", "run_retry_failed", "chained_ctx"),
            ("aap_migration.api.services.maintenance", "run_state_export", "workdir_ctx"),
        ],
    )
    def test_worker_uses_shared_lifecycle(
        self, module_name: str, worker: str, lifecycle: str, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """Behavioral proof: invoke the worker with stubbed lifecycle and
        assert the expected lifecycle entry was hit (not source text)."""
        import contextlib
        import importlib
        import types

        import aap_migration.api.services._core as core

        module = importlib.import_module(module_name)
        fn = getattr(module, worker)
        entered: list[str] = []

        @contextlib.contextmanager
        def _spy_chained(job: Any, **kwargs: Any) -> Any:
            entered.append("chained_ctx")
            cfg = types.SimpleNamespace(
                export=types.SimpleNamespace(records_per_file=1000),
                performance=types.SimpleNamespace(
                    project_patch_batch_size=50, project_patch_batch_interval=0
                ),
                state=types.SimpleNamespace(db_path=""),
                dry_run=False,
            )
            yield (types.SimpleNamespace(config=cfg), cfg, tmp_path, dict(job.get("params", {})))

        @contextlib.contextmanager
        def _spy_workdir(job: Any, **kwargs: Any) -> Any:
            entered.append("workdir_ctx")
            yield (tmp_path, dict(job.get("params", {})))

        monkeypatch.setattr(core, "chained_ctx", _spy_chained)
        monkeypatch.setattr(core, "workdir_ctx", _spy_workdir)
        try:
            monkeypatch.setattr(module, "chained_ctx", _spy_chained)
        except Exception:
            pass
        try:
            monkeypatch.setattr(module, "workdir_ctx", _spy_workdir)
        except Exception:
            pass
        # Stub worker-specific side effects so the lifecycle entry is
        # what is proven (failure after entry still proves entry).
        if module_name.endswith(".iam"):
            monkeypatch.setattr(
                module,
                "_iam_connections",
                lambda pdict, need="source": ({"url": "u", "token": "t"}, None),
            )
            for _stub in ("_run_iam_audit", "_run_iam_migrate", "_run_iam_benchmark"):
                try:
                    monkeypatch.setattr(module, _stub, lambda *a, **k: {"message": "ok"})
                except Exception:
                    pass
            try:
                monkeypatch.setattr("aap_migration.iam.benchmark.run_benchmark", lambda **k: None)
            except Exception:
                pass
        if worker == "run_retry_failed":
            monkeypatch.setattr(core, "call_command", lambda *a, **k: None)
            try:
                monkeypatch.setattr(module, "call_command", lambda *a, **k: None)
            except Exception:
                pass
            monkeypatch.setattr("aap_migration.api.context.write_job_config", lambda *a, **k: None)

            class _FakeState:
                database_url = "sqlite:///" + str(tmp_path / "r.db")

            import aap_migration.api.context as ctx_mod

            monkeypatch.setattr(ctx_mod, "open_default_state", lambda: _FakeState())
        if worker == "run_state_export":

            class _FakeState2:
                database_url = "sqlite:///" + str(tmp_path / "s.db")

                def export_state(self, path: str) -> None:
                    __import__("pathlib").Path(path).parent.mkdir(parents=True, exist_ok=True)
                    __import__("pathlib").Path(path).write_text("{}")

            monkeypatch.setattr(module, "open_default_state", lambda: _FakeState2())
        if worker == "run_iam_report":
            monkeypatch.setattr(
                module, "_iam_report_data", lambda *a, **k: {"report": "r"}
            ) if hasattr(module, "_iam_report_data") else None
        job = {"job_id": "lc", "job_dir": str(tmp_path), "params": {}}
        try:
            fn(job)
        except Exception:
            pass
        assert lifecycle in entered, f"{worker} did not enter {lifecycle}: {entered}"


class TestIamMigrateSchema:
    """#11: IamMigrateRequest inherits target_id (no bare redeclaration)."""

    def test_target_id_inherited_with_description(self) -> None:
        from aap_migration.api.schemas import (
            ConnectionSelector,
            IamMigrateRequest,
        )

        assert "target_id" not in IamMigrateRequest.__dict__.get("__annotations__", {})
        field = IamMigrateRequest.model_fields["target_id"]
        parent = ConnectionSelector.model_fields["target_id"]
        assert field.description == parent.description
        assert IamMigrateRequest().target_id is None
