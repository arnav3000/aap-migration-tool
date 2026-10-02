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
        self, module_name: str, worker: str, lifecycle: str
    ) -> None:
        import importlib
        import inspect

        module = importlib.import_module(module_name)
        assert f"{lifecycle}(" in inspect.getsource(getattr(module, worker))


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
