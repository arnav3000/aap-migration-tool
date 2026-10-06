"""Foundation regression tests for PR 141 review findings.

Covers the review findings without requiring the future
services/routers layers (stack 4/5):

- Schema validators work standalone (no services import).
- resolve_job_state maps unknown jobs to HTTP 404 (no routers import).
- Active-target moves are not vetoed by queued source-only jobs.
- Security auth gates + Fernet lifecycle (#6, #7).
- Store trust root: CRUD errors, need-scope, fingerprints (#8, #17, #18).
- Jobs core: capacity shed, cancel fence, scrub, execution fence (#4, #3).
- Schema 422 contracts (#14).
- Jobs package public surface has no privates (#11).
- CLI config seam + probe cooldown + resolve-once helpers (#10, #13, #2).
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aap_migration.api.jobs import reset_job_manager
from aap_migration.api.schemas._shared import parse_organizations


class TestParseOrganizations:
    def test_no_keys_means_all(self) -> None:
        assert parse_organizations({}) is None
        assert parse_organizations({"organizations": None, "orgs": None}) is None

    def test_canonical_list(self) -> None:
        assert parse_organizations({"organizations": ["a", "b"]}) == ["a", "b"]

    def test_comma_string(self) -> None:
        assert parse_organizations({"orgs": "a, b"}) == ["a", "b"]

    def test_single_spelling(self) -> None:
        assert parse_organizations({"organization": "a"}) == ["a"]

    def test_ambiguous_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_organizations({"organizations": ["a"], "orgs": "b"})

    def test_unknown_org_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_organizations({"organisations": ["a"]})


class TestSchemaValidatorsStandalone:
    """#2: validators must not import the future services layer."""

    def test_validate_request_orgs(self) -> None:
        from aap_migration.api.schemas.etl import ValidateRequest

        req = ValidateRequest(orgs="a, b")
        assert req.orgs == "a, b"

    def test_validate_request_ambiguous_rejected(self) -> None:
        from aap_migration.api.schemas.etl import ValidateRequest

        with pytest.raises(ValidationError):
            ValidateRequest(organizations=["a"], orgs="b")

    def test_analyze_requires_scope(self) -> None:
        from aap_migration.api.schemas.etl import AnalyzeDependenciesRequest

        with pytest.raises(ValidationError):
            AnalyzeDependenciesRequest()
        req = AnalyzeDependenciesRequest(analyze_all=True)
        assert req.analyze_all is True

    def test_enhanced_report_orgs(self) -> None:
        from aap_migration.api.schemas.etl import EnhancedReportRequest

        req = EnhancedReportRequest(orgs="a")
        assert req.orgs == "a"


class TestResolveJobStateMapping:
    """#1: job-scoped reads map to HTTP errors without routers layer."""

    def test_unknown_job_maps_to_404(self, tmp_path: Path) -> None:
        from aap_migration.api.context import resolve_job_state

        reset_job_manager(base_dir=str(tmp_path / "jobs"))
        with pytest.raises(HTTPException) as exc_info:
            resolve_job_state("does-not-exist")
        assert exc_info.value.status_code == 404


class TestActiveTargetVeto:
    """#14: source-only queued jobs must not veto an active-target move."""

    def _session(self, src: str | None, tgt: str | None) -> Any:
        active = SimpleNamespace(source_id=src, target_id=tgt)

        class FakeSession:
            def get(self, _model: Any, _pk: Any) -> Any:
                return active

        return FakeSession()

    def test_source_only_does_not_veto_target_move(self) -> None:
        from aap_migration.api import store
        from aap_migration.api.store import SNAPSHOT_NEED

        ref = {
            "job_type": "export",
            "params": {SNAPSHOT_NEED: "source"},
        }
        session = self._session("src-1", "tgt-1")
        with patch.object(store, "_pending_refs", return_value=[ref]):
            # Must not raise: the queued job never consumes the target.
            store._reject_if_active_referenced("tgt-1", session)

    def test_both_scope_still_vetoes_target_move(self) -> None:
        from aap_migration.api import store
        from aap_migration.api.jobs._records import ConflictError

        ref = {"job_type": "export", "params": {}}
        session = self._session("src-1", "tgt-1")
        with patch.object(store, "_pending_refs", return_value=[ref]):
            with pytest.raises(ConflictError):
                store._reject_if_active_referenced("tgt-1", session)


class TestApiKeyAuthGates:
    """#6: require_api_key fail-closed, rotation, and 429 containment."""

    def _req(self, ip: str) -> Any:
        return SimpleNamespace(client=SimpleNamespace(host=ip))

    def test_fail_closed_without_token(self, monkeypatch: Any) -> None:
        from aap_migration.api import security

        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN", raising=False)
        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
        monkeypatch.delenv("AAP_BRIDGE_ALLOW_ANON", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            security.require_api_key(None, None)
        assert exc_info.value.status_code == 401

    def test_allow_anon_opt_in(self, monkeypatch: Any) -> None:
        from aap_migration.api import security

        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN", raising=False)
        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
        monkeypatch.setenv("AAP_BRIDGE_ALLOW_ANON", "1")
        security.require_api_key(None, None)

    def test_primary_and_secondary_accepted(self, monkeypatch: Any) -> None:
        from aap_migration.api import security

        monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "primary-token-123456")
        monkeypatch.setenv("AAP_BRIDGE_API_TOKEN_SECONDARY", "secondary-token-123456")
        security.require_api_key("primary-token-123456", self._req("10.240.1.1"))
        security.require_api_key("secondary-token-123456", self._req("10.240.1.1"))

    def test_rotating_guesses_trip_ip_429(self, monkeypatch: Any) -> None:
        from aap_migration.api import security

        monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "primary-token-123456")
        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
        ip = "10.240.1.2"
        with pytest.raises(HTTPException) as first:
            security.require_api_key("wrong-0", self._req(ip))
        assert first.value.status_code == 401
        last_status = 401
        for i in range(1, 25):
            with pytest.raises(HTTPException) as exc_info:
                security.require_api_key(f"wrong-{i}", self._req(ip))
            last_status = exc_info.value.status_code
        assert last_status == 429

    def test_stale_key_contained_without_feeding_ip(self, monkeypatch: Any) -> None:
        from aap_migration.api import security

        monkeypatch.setenv("AAP_BRIDGE_API_TOKEN", "primary-token-123456")
        monkeypatch.delenv("AAP_BRIDGE_API_TOKEN_SECONDARY", raising=False)
        ip = "10.240.1.3"
        for _ in range(5):
            with pytest.raises(HTTPException) as exc_info:
                security.require_api_key("stale-looping-key", self._req(ip))
            assert exc_info.value.status_code == 401
        with pytest.raises(HTTPException) as exc_info:
            security.require_api_key("stale-looping-key", self._req(ip))
        assert exc_info.value.status_code == 429
        # Key-tier 429s do not feed the shared IP bucket (containment).
        assert security._auth_failure_count(ip) == 5


class TestFernetLifecycle:
    """#7: Fernet key errors map to actionable errors, round-trip works."""

    def test_invalid_env_key_is_internal_error(self, monkeypatch: Any) -> None:
        from aap_migration.api import security
        from aap_migration.api.jobs._records import InternalStatusError

        monkeypatch.setenv("AAP_BRIDGE_API_KEY", "not-a-valid-fernet-key")
        with pytest.raises(InternalStatusError):
            security.get_fernet()

    def test_round_trip(self, monkeypatch: Any) -> None:
        from cryptography.fernet import Fernet

        from aap_migration.api import security

        monkeypatch.setenv("AAP_BRIDGE_API_KEY", Fernet.generate_key().decode())
        assert security.decrypt_token(security.encrypt_token("s3cret")) == "s3cret"

    def test_wrong_key_decrypt_is_actionable(self, monkeypatch: Any) -> None:
        from cryptography.fernet import Fernet

        from aap_migration.api import security

        monkeypatch.setenv("AAP_BRIDGE_API_KEY", Fernet.generate_key().decode())
        cipher = security.encrypt_token("s3cret")
        monkeypatch.setenv("AAP_BRIDGE_API_KEY", Fernet.generate_key().decode())
        with pytest.raises(ValueError, match="re-create"):
            security.decrypt_token(cipher)


class TestStoreTrustRoot:
    """#8: CRUD errors, need-scope resolution, fingerprint drift."""

    SRC_URL = "https://aap-source.example.com/api"
    TGT_URL = "https://aap-target.example.com/api"

    def _db(self, tmp_path: Path, monkeypatch: Any) -> str:
        monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
        return str(tmp_path / "api.db")

    def _make(self, db: str, name: str, kind: str, url: str) -> dict[str, Any]:
        from aap_migration.api import store

        return store.create_connection(name, kind, url, token=f"token-for-{name}", db_path=db)

    def test_duplicate_name_rejected(self, tmp_path: Path, monkeypatch: Any) -> None:
        db = self._db(tmp_path, monkeypatch)
        self._make(db, "c1", "source", self.SRC_URL)
        with pytest.raises(ValueError, match="already exists"):
            self._make(db, "c1", "source", self.SRC_URL)

    def test_unknown_id_raises_key_error(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store

        db = self._db(tmp_path, monkeypatch)
        with pytest.raises(KeyError):
            store.get_connection("missing-id", db_path=db)

    def test_kind_mismatch_rejected(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store

        db = self._db(tmp_path, monkeypatch)
        tgt = self._make(db, "t1", "target", self.TGT_URL)
        with pytest.raises(ValueError, match="not a source"):
            store.set_active(source_id=tgt["id"], db_path=db)

    def test_need_source_tolerates_missing_target(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        src = self._make(db, "s1", "source", self.SRC_URL)
        store.set_active(source_id=src["id"], db_path=db)
        source, target = store.resolve_active_pair(
            ConnectionScope(source_id=src["id"], db_path=db, need="source")
        )
        assert source is not None
        assert source["id"] == src["id"]
        assert target is None

    def test_need_both_requires_target(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        src = self._make(db, "s1", "source", self.SRC_URL)
        store.set_active(source_id=src["id"], db_path=db)
        with pytest.raises(ValueError, match="No target"):
            store.resolve_active_pair(ConnectionScope(source_id=src["id"], db_path=db, need="both"))

    def test_fingerprint_changes_on_rotation(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        src = self._make(db, "s1", "source", self.SRC_URL)
        tgt = self._make(db, "t1", "target", self.TGT_URL)
        scope = ConnectionScope(source_id=src["id"], target_id=tgt["id"], db_path=db, need="both")
        before = store.pair_fingerprint(scope)["fp"]
        store.update_connection(src["id"], token="rotated-token", db_path=db)
        after = store.pair_fingerprint(scope)["fp"]
        assert before != after

    def test_none_scope_is_source_independent(self, tmp_path: Path, monkeypatch: Any) -> None:
        """#17: connectionless pins never touch the store."""
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        scope = ConnectionScope(db_path=db, need="none")
        snap = store.pair_fingerprint(scope)
        assert snap["source_id"] is None
        assert snap["target_id"] is None
        source, target = store.resolve_active_pair(scope)
        assert source is None
        assert target is None

    def test_case_only_url_edit_keeps_fingerprint(self, tmp_path: Path, monkeypatch: Any) -> None:
        """#18: scheme/host case is canonicalized in the fingerprint."""
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        src = self._make(db, "s1", "source", "https://AAP-Upper.example.com/api")
        scope = ConnectionScope(source_id=src["id"], db_path=db, need="source")
        before = store.pair_fingerprint(scope)["fp"]
        store.update_connection(src["id"], url="https://aap-upper.example.com/api", db_path=db)
        assert store.pair_fingerprint(scope)["fp"] == before


class TestJobsCore:
    """#4: capacity shed, cancel fence, scrub. #3: execution-time fence."""

    def _cancelled_parent(self, mgr: Any, tmp_path: Path) -> str:
        job_dir = tmp_path / "jobs" / "parent-1"
        job_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "job_id": "parent-1",
            "job_type": "export",
            "status": "cancelled",
            "params": {},
            "job_dir": str(job_dir),
            "result": None,
            "error": "Cancelled by operator",
            "error_id": None,
            "exit_code": None,
            "created_at": "t",
            "updated_at": "t",
            "cancel_requested": True,
            "cancelled_at_phase": "export",
            "completed_phases": [],
        }
        mgr._jobs["parent-1"] = record
        return "parent-1"

    def test_submit_sheds_at_capacity(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import _config as job_config
        from aap_migration.api.jobs import reset_job_manager
        from aap_migration.api.jobs._records import QueueFullError

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        with patch.object(job_config, "MAX_QUEUE_DEPTH", 0):
            with pytest.raises(QueueFullError):
                mgr.submit("export", {}, lambda job: {})

    def test_submit_rejects_base_dir_itself(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import reset_job_manager

        base = str(tmp_path / "jobs")
        mgr = reset_job_manager(base_dir=base)
        with pytest.raises(ValueError, match="strictly under"):
            mgr.submit("export", {}, lambda job: {}, job_dir=base)

    def test_cancel_fence_blocks_without_force(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import reset_job_manager
        from aap_migration.api.jobs._records import ConflictError

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        ref = self._cancelled_parent(mgr, tmp_path)
        with pytest.raises(ConflictError):
            mgr.assert_no_cancel_fence(ref, False)
        mgr.assert_no_cancel_fence(ref, True)
        mgr.assert_no_cancel_fence(None, False)
        mgr.assert_no_cancel_fence("unknown-id", False)

    def test_setup_chained_rechecks_cancel_fence(self, tmp_path: Path) -> None:
        """#3: a cancel landing after submit fails execution, not replay."""
        from aap_migration.api import context as api_context
        from aap_migration.api.jobs import reset_job_manager
        from aap_migration.api.jobs._records import ConflictError

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        ref = self._cancelled_parent(mgr, tmp_path)
        params: dict[str, Any] = {"job_id": ref, "force": False}
        with pytest.raises(ConflictError):
            api_context.setup_chained(
                params,
                str(tmp_path / "jobs" / "child"),
                allow_statuses=("succeeded", "failed", "cancelled"),
            )

    def test_scrub_redacts_secrets(self) -> None:
        from aap_migration.api.jobs._scrub import _scrub_output

        assert "abc.def" not in _scrub_output("Authorization: Bearer abc.def.ghi")
        assert "secret123" not in _scrub_output("token=secret123")
        scrubbed = _scrub_output("https://user:pass@host.example/x")
        assert "user:pass" not in scrubbed
        assert "://***@" in scrubbed


class TestSchemaContracts:
    """#14: request validators reject bad payloads with 422-style errors."""

    def test_migrate_rejects_phase3(self) -> None:
        from aap_migration.api.schemas.etl import MigrateRequest

        with pytest.raises(ValidationError):
            MigrateRequest(phase="phase3")

    def test_migrate_lowercases_phase(self) -> None:
        from aap_migration.api.schemas.etl import MigrateRequest

        assert MigrateRequest(phase="PHASE1").phase == "phase1"

    def test_iam_migrate_rejects_exclusive_flags(self) -> None:
        from aap_migration.api.schemas.etl import IamMigrateRequest

        with pytest.raises(ValidationError):
            IamMigrateRequest(skip_user_roles=True, users_only=True)

    def test_iam_benchmark_rejects_out_of_range_workers(self) -> None:
        from aap_migration.api.schemas.etl import IamBenchmarkRequest

        with pytest.raises(ValidationError):
            IamBenchmarkRequest(workers=[0])
        with pytest.raises(ValidationError):
            IamBenchmarkRequest(workers=[65])
        assert IamBenchmarkRequest(workers=[1, 64]).workers == [1, 64]

    def test_iam_report_requires_selector(self) -> None:
        from aap_migration.api.schemas.etl import IamReportRequest

        with pytest.raises(ValidationError):
            IamReportRequest()
        assert IamReportRequest(job_id="j1").job_id == "j1"

    def test_extra_fields_rejected(self) -> None:
        from aap_migration.api.schemas.etl import IamBenchmarkRequest, MigrateRequest

        with pytest.raises(ValidationError):
            MigrateRequest(phase="all", resource_typs=["x"])
        with pytest.raises(ValidationError):
            IamBenchmarkRequest(workers=[1], bogus=1)


class TestJobsPublicSurface:
    """#11: leaf privates are not re-exported from the jobs package."""

    def test_no_underscore_helpers_exported(self) -> None:
        import aap_migration.api.jobs as pkg

        for name in (
            "_scrub_output",
            "_bounded_output",
            "_console_tail",
            "_persist_console",
            "_store_http_error",
            "_utcnow",
            "_normalize_result",
            "_ThreadLocalProxy",
        ):
            assert name not in pkg.__all__
            assert not hasattr(pkg, name)

    def test_public_names_still_exported(self) -> None:
        import aap_migration.api.jobs as pkg

        for name in (
            "JobManager",
            "JobRecord",
            "JobUpdate",
            "get_job_manager",
            "reset_job_manager",
            "manager_or_none",
            "read_console_tail",
            "public_job_params",
            "QueueFullError",
            "ConflictError",
        ):
            assert name in pkg.__all__
            assert hasattr(pkg, name)


class TestFoundationSeams:
    """#10 CLI seam, #13 probe cooldown, #2 resolve-once helper."""

    def test_replace_config_seam(self) -> None:
        from pathlib import Path as _Path

        from aap_migration.cli.context import MigrationContext

        ctx = MigrationContext(config_path=_Path("."), log_level="ERROR")
        sentinel: Any = SimpleNamespace(marker=True)
        ctx.replace_config(sentinel)
        assert ctx._config is sentinel

    def test_close_clients_without_clients(self) -> None:
        from pathlib import Path as _Path

        from aap_migration.cli.context import MigrationContext

        ctx = MigrationContext(config_path=_Path("."), log_level="ERROR")
        ctx.close_clients()  # must not raise

    def test_probe_cooldown_skips_spawn(self, tmp_path: Path) -> None:
        """#13: a cooling manager 503s without stranding another thread."""
        import time

        from aap_migration.api.jobs import reset_job_manager

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        mgr._probe_cooldown_until = time.monotonic() + 60.0
        assert mgr._clear_degraded_bounded() is False
        assert mgr._probe_event is None

    def test_fingerprint_of_records_matches_pair(self, tmp_path: Path, monkeypatch: Any) -> None:
        """#2: the helper fingerprints exactly what the pair holds."""
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
        db = str(tmp_path / "api.db")
        src = store.create_connection(
            "s1",
            "source",
            "https://aap-source.example.com/api",
            token="tok-s",
            db_path=db,
        )
        tgt = store.create_connection(
            "t1",
            "target",
            "https://aap-target.example.com/api",
            token="tok-t",
            db_path=db,
        )
        scope = ConnectionScope(source_id=src["id"], target_id=tgt["id"], db_path=db, need="both")
        source, target = store.resolve_active_pair(scope)
        assert store.pair_fingerprint(scope) == store.fingerprint_of_records(source, target, "both")


class TestSourceOnlyContextBuild:
    """P1 #1: source-only jobs build a context without a target."""

    def _rec(self, url: str) -> dict[str, Any]:
        return {"url": url, "token": "tok", "verify_ssl": True, "timeout": 30}

    def test_source_only_builds_without_target(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from aap_migration.api import context as api_context

        src = self._rec("https://aap-source.example.com/api")
        with patch(
            "aap_migration.utils.ssrf.reverify_execution_url_bounded",
            lambda url: None,
        ):
            ctx, config = api_context._build_job_context_from_records(
                str(tmp_path / "job1"), src, None, "source"
            )
        assert ctx is not None
        assert config is not None

    def test_both_still_requires_target(self, tmp_path: Path) -> None:
        from aap_migration.api import context as api_context

        src = self._rec("https://aap-source.example.com/api")
        with pytest.raises(ValueError):
            api_context._build_job_context_from_records(str(tmp_path / "job1"), src, None, "both")


class TestInferNeed:
    """P2 #14: one validated need inference, fail closed on corrupt values."""

    def test_defaults(self) -> None:
        from aap_migration.api import context as api_context
        from aap_migration.api.store import SNAPSHOT_NEED

        assert api_context._infer_need({SNAPSHOT_NEED: "both"}, "t") == "both"
        assert api_context._infer_need({SNAPSHOT_NEED: "none"}, None) == "none"
        assert api_context._infer_need({}, None) == "source"
        assert api_context._infer_need({}, "t") == "both"

    def test_corrupt_fails_closed(self) -> None:
        from aap_migration.api import context as api_context
        from aap_migration.api.store import SNAPSHOT_FP, SNAPSHOT_NEED

        with pytest.raises(ValueError, match="corrupt"):
            api_context._infer_need({SNAPSHOT_NEED: "bogus"}, "t")
        # verify_execution_pair must fail closed too, not skip the TLS veto.
        with pytest.raises(ValueError, match="corrupt"):
            api_context.verify_execution_pair({SNAPSHOT_FP: "x", SNAPSHOT_NEED: "bogus"})


class TestAnalyzeEmptyScope:
    """P2 #11: explicit [] is a no-op selecting none."""

    def test_explicit_empty_is_noop(self) -> None:
        from aap_migration.api.schemas.etl import AnalyzeDependenciesRequest

        req = AnalyzeDependenciesRequest(organizations=[])
        assert req.organizations == []

    def test_omitted_still_requires_analyze_all(self) -> None:
        from pydantic import ValidationError

        from aap_migration.api.schemas.etl import AnalyzeDependenciesRequest

        with pytest.raises(ValidationError):
            AnalyzeDependenciesRequest()

    def test_analyze_all_rejects_explicit_empty(self) -> None:
        from pydantic import ValidationError

        from aap_migration.api.schemas.etl import AnalyzeDependenciesRequest

        with pytest.raises(ValidationError):
            AnalyzeDependenciesRequest(organizations=[], analyze_all=True)


class TestFenceTracker:
    """P1 #3/#4: submit/wait/reap branches + target-side fencing."""

    def _tracker(self, max_orphans: int = 2) -> Any:
        from aap_migration.api.jobs._fences import FenceTracker

        return FenceTracker(max_orphans=lambda: max_orphans, job_timeout=lambda: 60.0)

    def _orphan(self, done: bool = False) -> Any:
        return SimpleNamespace(
            done=lambda: done,
            pool=SimpleNamespace(shutdown=lambda **kwargs: None),
        )

    def test_orphan_cap_sheds_and_wait_fails_fast(self) -> None:
        tr = self._tracker(max_orphans=1)
        tr.note_timeout(
            future=self._orphan(),
            pool=self._orphan(),
            job_dir="/d/1",
            pair_fp="fp-a",
            stable_fp="st-a",
        )
        assert "Too many timed-out" in (tr.check_submit("/d/2", "fp-b", "st-b") or "")
        assert tr.wait_unfenced("fp-b", "pair") is False

    def test_workdir_and_pair_fences(self) -> None:
        tr = self._tracker()
        tr.note_timeout(
            future=self._orphan(),
            pool=self._orphan(),
            job_dir="/d/1",
            pair_fp="fp-a",
            stable_fp="st-a",
        )
        assert "Workdir fenced" in (tr.check_submit("/d/1", "fp-b", "st-b") or "")
        assert "AAP pair fenced" in (tr.check_submit("/d/2", "fp-a") or "")
        assert "AAP pair fenced" in (tr.check_submit("/d/2", "other", "st-a") or "")
        assert tr.check_submit("/d/2", "fp-b", "st-b") is None

    def test_target_side_fence_blocks_different_source(self) -> None:
        """P1 #4: B->T must not run while the A->T orphan still writes."""
        tr = self._tracker()
        tr.note_timeout(
            future=self._orphan(),
            pool=self._orphan(),
            job_dir="/d/a",
            pair_fp="fp-a",
            stable_fp="st-a",
            target_stable="tgt-T",
        )
        assert "target fenced" in (tr.check_submit("/d/b", "fp-b", "st-b", "tgt-T") or "")
        assert tr.is_fenced_target("tgt-T") is True
        # Fresh target is unaffected; jobs without a target never block.
        assert tr.check_submit("/d/b", "fp-b", "st-b", "tgt-other") is None
        assert tr.check_submit("/d/b", "fp-b", "st-b", None) is None
        assert tr.is_fenced_target(None) is False

    def test_reap_holds_fence_past_deadline_until_done(self) -> None:
        import time

        tr = self._tracker()
        fut = self._orphan(done=False)
        tr.note_timeout(
            future=fut,
            pool=self._orphan(),
            job_dir="/d/1",
            pair_fp="fp-a",
            stable_fp="st-a",
            target_stable="tgt-T",
        )
        # Force the deadline into the past: fence must still hold.
        with tr._lock:
            tr._orphans[0]["fence_expires_at"] = time.monotonic() - 1.0
        tr.reap()
        assert tr.is_fenced_dir("/d/1") is True
        assert tr.is_fenced_target("tgt-T") is True
        # Once the thread finishes, reap releases everything.
        fut.done = lambda: True
        tr.reap()
        assert tr.is_fenced_dir("/d/1") is False
        assert tr.is_fenced_target("tgt-T") is False
        assert tr.check_submit("/d/1", "fp-a", "st-a", "tgt-T") is None


class TestRetention:
    """P1 #5: sweep/evict/confinement paths."""

    def test_sweep_quarantines_only_aged_recordless_dirs(self, tmp_path: Path) -> None:
        import os
        import time

        from aap_migration.api.jobs import _retention as retention

        base = tmp_path / "jobs"
        base.mkdir()
        live = base / "live-1"
        live.mkdir()
        fenced = base / "fenced-1"
        fenced.mkdir()
        recent = base / "recent-1"
        recent.mkdir()
        aged = base / "orphan-1"
        aged.mkdir()
        old = time.time() - 3600.0
        os.utime(aged, (old, old))
        dot = base / ".hidden"
        dot.mkdir()
        (base / "file.txt").write_text("x")

        moved, _removed = retention.sweep_orphan_dirs(
            str(base),
            {str(live)},
            {str(fenced)},
            max_age_secs=600.0,
            quarantine_max=10,
        )
        assert moved == 1
        assert not aged.exists()
        assert (base / ".orphan-quarantine").exists()
        assert live.exists() and fenced.exists() and recent.exists()
        assert dot.exists()

    def test_sweep_respects_quarantine_cap(self, tmp_path: Path) -> None:
        import os
        import time

        from aap_migration.api.jobs import _retention as retention

        base = tmp_path / "jobs"
        base.mkdir()
        old = time.time() - 3600.0
        for i in range(3):
            d = base / f"orphan-{i}"
            d.mkdir()
            os.utime(d, (old, old))
        moved, removed = retention.sweep_orphan_dirs(
            str(base),
            set(),
            set(),
            max_age_secs=600.0,
            quarantine_max=1,
        )
        assert moved == 3
        assert removed == 2
        assert len(list((base / ".orphan-quarantine").iterdir())) == 1

    def test_remove_job_dir_confinement(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import _retention as retention

        base = tmp_path / "jobs"
        base.mkdir()
        victim = base / "gone-1"
        victim.mkdir()
        retention.remove_job_dir(str(victim), str(base))
        assert not victim.exists()
        # Base dir itself and traversal escapes are no-ops.
        retention.remove_job_dir(str(base), str(base))
        assert base.exists()
        retention.remove_job_dir(str(tmp_path / "escape"), str(base))
        assert not (tmp_path / "escape").exists()

    def test_evict_keeps_fenced_and_shared_dirs(self) -> None:
        from aap_migration.api.jobs import _retention as retention
        from aap_migration.api.jobs._records import ACTIVE_STATUSES, TERMINAL_STATUSES

        def _job(jid: str, status: str, job_dir: str, created: str) -> dict[str, Any]:
            return {
                "job_id": jid,
                "job_type": "export",
                "status": status,
                "params": {},
                "job_dir": job_dir,
                "created_at": created,
                "updated_at": created,
            }

        jobs = {
            "old": _job("old", "succeeded", "/base/old", "2024-01-01T00:00:00"),
            "mid": _job("mid", "failed", "/base/fenced", "2024-01-02T00:00:00"),
            "run": _job("run", "running", "/base/old", "2024-01-03T00:00:00"),
        }
        evicted = retention.evict_locked(
            jobs,
            {},
            {},
            {"/base/fenced"},
            2,
            ACTIVE_STATUSES,
            TERMINAL_STATUSES,
        )
        # The oldest terminal record is evicted, but no directory is
        # removed: /base/old is shared with a running job and /base/fenced
        # is fenced.
        assert "old" not in jobs and "mid" in jobs
        assert "/base/old" not in evicted
        assert "/base/fenced" not in evicted
        assert evicted == ()


class TestWorkerCancelStopPoint:
    """P1 #6: cancel fence markers derive from attempt results."""

    def test_steps_completed_mapping(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import reset_job_manager

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        phase, completed = mgr._cancel_stop_point(
            {"job_type": "import", "status": "running"}, {"steps_completed": ["a", "b"]}
        )
        assert phase == "import"
        assert completed == ["a", "b"]

    def test_non_dict_result_and_missing_type(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import reset_job_manager

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        assert mgr._cancel_stop_point({"job_type": "export"}, None) == ("export", [])
        assert mgr._cancel_stop_point({}, "oops") == ("unknown", [])
        assert mgr._cancel_stop_point({}, {"steps_completed": 5}) == ("unknown", [])


class TestFernetKeyFile:
    """P1 #8: key-file creation race, invalid content, non-file DB."""

    def _env(self, monkeypatch: Any, tmp_path: Path, key: str | None) -> str:
        if key is None:
            monkeypatch.delenv("AAP_BRIDGE_API_KEY", raising=False)
        else:
            monkeypatch.setenv("AAP_BRIDGE_API_KEY", key)
        db = str(tmp_path / "keys" / "api.db")
        monkeypatch.setenv("AAP_BRIDGE_API_DB", db)
        return db

    def test_file_key_created_with_0600_and_round_trips(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import hashlib
        import os

        from aap_migration.api import security

        self._env(monkeypatch, tmp_path, None)
        cipher = security.encrypt_token("s3cret")
        assert security.decrypt_token(cipher) == "s3cret"
        key_file = tmp_path / "keys" / "api_fernet.key"
        assert key_file.exists()
        assert os.stat(key_file).st_mode & 0o777 == 0o600
        assert (
            security.fernet_key_fingerprint()
            == hashlib.sha256(key_file.read_bytes().strip()).hexdigest()
        )

    def test_invalid_key_file_is_actionable(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import security
        from aap_migration.api._errors import InternalStatusError

        self._env(monkeypatch, tmp_path, None)
        key_file = tmp_path / "keys" / "api_fernet.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(b"not-a-valid-key")
        with pytest.raises(InternalStatusError):
            security.get_fernet()

    def test_non_file_db_without_env_key_fails_closed(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from aap_migration.api import security

        monkeypatch.delenv("AAP_BRIDGE_API_KEY", raising=False)
        monkeypatch.setenv("AAP_BRIDGE_API_DB", "postgresql://db/api")
        with pytest.raises(ValueError, match="AAP_BRIDGE_API_KEY"):
            security.get_fernet()

    def test_env_key_fingerprint(self, monkeypatch: Any) -> None:
        import hashlib

        from cryptography.fernet import Fernet

        from aap_migration.api import security

        raw = Fernet.generate_key().decode()
        monkeypatch.setenv("AAP_BRIDGE_API_KEY", raw)
        assert security.fernet_key_fingerprint() == hashlib.sha256(raw.encode()).hexdigest()


class TestStorePostureAndVeto:
    """P1 #9: posture guards, TLS/timeout drift, pin vetoes."""

    SRC_URL = "https://aap-source.example.com/api"
    TGT_URL = "https://aap-target.example.com/api"

    def _db(self, tmp_path: Path, monkeypatch: Any) -> str:
        import cryptography.fernet as _fernet_mod

        monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
        monkeypatch.setenv("AAP_BRIDGE_API_KEY", _fernet_mod.Fernet.generate_key().decode())
        db = str(tmp_path / "api.db")
        # verify_execution_pair / check_tls_posture resolve through the
        # env-default DB, mirroring production wiring.
        monkeypatch.setenv("AAP_BRIDGE_API_DB", db)
        return db

    def _pair(self, db: str) -> tuple[dict[str, Any], dict[str, Any]]:
        from aap_migration.api import store

        src = store.create_connection("s1", "source", self.SRC_URL, token="tok-s", db_path=db)
        tgt = store.create_connection("t1", "target", self.TGT_URL, token="tok-t", db_path=db)
        store.set_active(source_id=src["id"], target_id=tgt["id"], db_path=db)
        return src, tgt

    def test_stored_posture_has_no_token_material(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        src, tgt = self._pair(db)
        posture = store.stored_posture(
            ConnectionScope(source_id=src["id"], target_id=tgt["id"], db_path=db, need="both")
        )
        assert set(posture) == {"source", "target"}
        assert "token" not in posture["source"] and "token" not in posture["target"]
        src_only = store.stored_posture(
            ConnectionScope(source_id=src["id"], db_path=db, need="source")
        )
        assert set(src_only) == {"source"}

    def test_tls_weakening_fails_closed(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import context as api_context

        db = self._db(tmp_path, monkeypatch)
        src, tgt = self._pair(db)
        with pytest.raises(ValueError, match="verify_ssl"):
            api_context.check_tls_posture(
                {"verify_ssl": False, "source_id": src["id"], "target_id": tgt["id"]},
                "both",
            )
        # Connectionless jobs skip; strengthening passes.
        api_context.check_tls_posture({"verify_ssl": False}, "none")

    def test_tls_and_timeout_edits_drift_fingerprint(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        db = self._db(tmp_path, monkeypatch)
        src, tgt = self._pair(db)
        scope = ConnectionScope(source_id=src["id"], target_id=tgt["id"], db_path=db, need="both")
        before = store.pair_fingerprint(scope)["fp"]
        store.update_connection(src["id"], verify_ssl=False, db_path=db)
        assert store.pair_fingerprint(scope)["fp"] != before
        weakened = store.pair_fingerprint(scope)["fp"]
        store.update_connection(src["id"], verify_ssl=True, timeout=99, db_path=db)
        assert store.pair_fingerprint(scope)["fp"] != weakened

    def test_pinned_connection_delete_is_vetoed(self, tmp_path: Path, monkeypatch: Any) -> None:
        from aap_migration.api import store
        from aap_migration.api._errors import ConflictError
        from aap_migration.api.store import SNAPSHOT_SOURCE_ID

        db = self._db(tmp_path, monkeypatch)
        src, _tgt = self._pair(db)
        ref = {"job_type": "export", "params": {SNAPSHOT_SOURCE_ID: src["id"]}}
        with pytest.raises(ConflictError):
            store._reject_if_connection_referenced(
                src["id"], self._session("x", "y"), pending_refs=lambda: [ref]
            )

    def _session(self, src: str | None, tgt: str | None) -> Any:
        active = SimpleNamespace(source_id=src, target_id=tgt)

        class FakeSession:
            def get(self, _model: Any, _pk: Any) -> Any:
                return active

        return FakeSession()


class TestExecutionDriftGuards:
    """P1 #2: fernet rotation, deleted connections, pair-switch drift."""

    SRC_URL = "https://aap-source.example.com/api"
    TGT_URL = "https://aap-target.example.com/api"

    def _db(self, tmp_path: Path, monkeypatch: Any, key: str) -> str:
        monkeypatch.setenv("AAP_BRIDGE_SSRF_ALLOW_PRIVATE", "1")
        monkeypatch.setenv("AAP_BRIDGE_API_KEY", key)
        db = str(tmp_path / "api.db")
        # verify_execution_pair resolves through the env-default DB,
        # mirroring production wiring.
        monkeypatch.setenv("AAP_BRIDGE_API_DB", db)
        return db

    def _snap(self, db: str) -> dict[str, Any]:
        from aap_migration.api import context as api_context
        from aap_migration.api import store
        from aap_migration.api.store import ConnectionScope

        src = store.create_connection("s1", "source", self.SRC_URL, token="tok-s", db_path=db)
        tgt = store.create_connection("t1", "target", self.TGT_URL, token="tok-t", db_path=db)
        store.set_active(source_id=src["id"], target_id=tgt["id"], db_path=db)
        return api_context.submit_pair_snapshot(
            ConnectionScope(source_id=src["id"], target_id=tgt["id"], db_path=db, need="both")
        )

    def test_fernet_rotation_fails_fast(self, tmp_path: Path, monkeypatch: Any) -> None:
        from cryptography.fernet import Fernet

        from aap_migration.api import context as api_context

        db = self._db(tmp_path, monkeypatch, Fernet.generate_key().decode())
        snap = self._snap(db)
        monkeypatch.setenv("AAP_BRIDGE_API_KEY", Fernet.generate_key().decode())
        with pytest.raises(ValueError, match="drain the queue"):
            api_context.verify_execution_pair(snap)

    def test_deleted_connection_maps_to_resubmit(self, tmp_path: Path, monkeypatch: Any) -> None:
        from cryptography.fernet import Fernet

        from aap_migration.api import context as api_context
        from aap_migration.api import store

        db = self._db(tmp_path, monkeypatch, Fernet.generate_key().decode())
        snap = self._snap(db)
        # Delete the pinned target through the store.
        from aap_migration.api.store import SNAPSHOT_TARGET_ID

        store.delete_connection(
            [c["id"] for c in store.list_connections(db_path=db) if c["kind"] == "target"][0],
            db_path=db,
        )
        assert snap[SNAPSHOT_TARGET_ID]
        with pytest.raises(ValueError, match="deleted|resubmit"):
            api_context.verify_execution_pair(snap)

    def test_pair_switch_id_change_with_drift_still_fails(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from cryptography.fernet import Fernet

        from aap_migration.api import context as api_context
        from aap_migration.api import store

        db = self._db(tmp_path, monkeypatch, Fernet.generate_key().decode())
        snap = self._snap(db)
        snap["allow_pair_switch"] = True
        store.update_connection(
            [c["id"] for c in store.list_connections(db_path=db) if c["kind"] == "source"][0],
            token="rotated-token",
            db_path=db,
        )
        with pytest.raises(ValueError, match="resubmit"):
            api_context.verify_execution_pair(snap)


class TestParkedPairBound:
    """P1 #7: same-pair parked bursts shed instead of freezing the queue."""

    def test_same_pair_parks_shed_past_bound(self, tmp_path: Path) -> None:
        from aap_migration.api.jobs import _config as job_config
        from aap_migration.api.jobs import reset_job_manager
        from aap_migration.api.jobs._records import QueueFullError
        from aap_migration.api.store import SNAPSHOT_FP

        mgr = reset_job_manager(base_dir=str(tmp_path / "jobs"))
        mgr._parked["old"] = {
            "work_dir": "/base/old",
            "pair_fp": "fp-1",
            "stable_fp": "",
            "target_stable": "",
            "fence": "pair",
            "until": 0.0,
        }
        with patch.object(job_config, "MAX_PARKED_PER_PAIR", 1):
            with pytest.raises(QueueFullError, match="same fenced pair"):
                mgr.submit("export", {SNAPSHOT_FP: "fp-1"}, lambda job: {})
            # An unrelated pair still admits.
            job = mgr.submit("export", {SNAPSHOT_FP: "fp-9"}, lambda job: {})
            assert job["status"] == "queued"


class TestFoundationHygiene:
    """P2 hygiene: dead code gone, errors module, constants, logging registry."""

    def test_dead_helpers_removed(self) -> None:
        from aap_migration.api import security

        # confine_path / redact_backend_error were restored: routers and
        # services in this stack call them (jobs/iam artifact confinement,
        # config/connections 502 redaction).
        assert not hasattr(security, "_sweep_auth_failures")
        assert callable(security.confine_path)
        assert callable(security.redact_backend_error)

    def test_errors_live_in_neutral_module(self) -> None:
        import aap_migration.api._errors as errors
        import aap_migration.api.jobs._records as records

        for name in (
            "ConflictError",
            "InternalStatusError",
            "QueueFullError",
            "ServerShuttingDownError",
            "StorageUnhealthyError",
            "UnknownJobError",
            "WorkdirGoneError",
        ):
            assert getattr(errors, name) is getattr(records, name), name
        assert callable(errors._store_http_error)

    def test_param_constants(self) -> None:
        from aap_migration.api import store

        assert store.PARAM_JOB_ID == "job_id"
        assert store.PARAM_ALLOW_PAIR_SWITCH == "allow_pair_switch"
        assert store.PARAM_FORCE == "force"
        assert store.SNAPSHOT_TARGET_STABLE == "_snapshot_target_stable"

    def test_logging_registry_no_monkeypatch(self, tmp_path: Path) -> None:
        import logging

        from aap_migration.api import context as api_context

        logger = logging.getLogger("aap_migration.api.job")
        before = len(logger.handlers)
        api_context.setup_job_logging(str(tmp_path / "job1"))
        assert len(logger.handlers) == before + 1
        api_context.setup_job_logging(str(tmp_path / "job1"))
        assert len(logger.handlers) == before + 1
        api_context.teardown_job_logging(str(tmp_path / "job1"))
        assert len(logger.handlers) == before
