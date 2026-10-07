"""Foundation regression tests for PR 141 review findings.

Covers the four actionable findings without requiring the future
services/routers layers (stack 4/5):

- #2: schema validators work standalone (no services._core import).
- #1: resolve_job_state maps unknown jobs to HTTP 404 (no routers import).
- #14: active-target moves are not vetoed by queued source-only jobs.
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
