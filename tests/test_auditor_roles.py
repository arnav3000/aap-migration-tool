"""Tests for Gateway Platform Auditor post-import assignments."""

from __future__ import annotations

import pytest

from aap_migration.migration.auditor_roles import (
    AuditorAssignmentResult,
    AuditorRolesSummary,
    assign_auditor_roles,
    create_preflight_failure_summary,
    preflight_gateway_access,
)


def test_create_preflight_failure_summary_marks_all_failed() -> None:
    auditors = [{"username": "alice", "source_id": 1, "id": 1}]
    summary = create_preflight_failure_summary(auditors, "403 forbidden")
    assert summary.auditor_count == 1
    assert len(summary.failed) == 1
    assert summary.failed[0].success is False
    assert "403 forbidden" in summary.failed[0].error or ""


@pytest.mark.asyncio
async def test_preflight_gateway_access_resolves_role_definition() -> None:
    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"id": 42, "name": "Platform Auditor"}]}

    class FakeHTTPClient:
        async def get(self, endpoint, params=None, headers=None):
            assert "role_definitions" in endpoint
            assert params == {"name": "Platform Auditor"}
            return FakeResponse()

    client = type(
        "Client",
        (),
        {
            "base_url": "https://aap.example.com/api/controller/v2",
            "token": "secret",
            "client": FakeHTTPClient(),
        },
    )()

    role_id = await preflight_gateway_access(client)
    assert role_id == 42


@pytest.mark.asyncio
async def test_preflight_gateway_access_raises_on_auth_failure() -> None:
    class FakeResponse:
        status_code = 403

    class FakeHTTPClient:
        async def get(self, endpoint, params=None, headers=None):
            return FakeResponse()

    client = type(
        "Client",
        (),
        {
            "base_url": "https://aap.example.com/api/controller/v2",
            "token": "secret",
            "client": FakeHTTPClient(),
        },
    )()

    with pytest.raises(RuntimeError, match="Gateway API returned 403"):
        await preflight_gateway_access(client)


@pytest.mark.asyncio
async def test_preflight_gateway_access_raises_on_unauthorized() -> None:
    class FakeResponse:
        status_code = 401

    class FakeHTTPClient:
        async def get(self, endpoint, params=None, headers=None):
            return FakeResponse()

    client = type(
        "Client",
        (),
        {
            "base_url": "https://aap.example.com/api/controller/v2",
            "token": "secret",
            "client": FakeHTTPClient(),
        },
    )()

    with pytest.raises(RuntimeError, match="Gateway API returned 401"):
        await preflight_gateway_access(client)


@pytest.mark.asyncio
async def test_assign_auditor_roles_verifies_controller_sync() -> None:
    class FakeGWResponse:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": 900}

    class FakeHTTPClient:
        def __init__(self) -> None:
            self.polls = 0

        async def post(self, endpoint, json=None, headers=None):
            assert "role_user_assignments" in endpoint
            return FakeGWResponse()

    class FakeTargetClient:
        def __init__(self) -> None:
            self.base_url = "https://aap.example.com/api/controller/v2"
            self.token = "secret"
            self.http = FakeHTTPClient()
            self.client = self.http
            self.polls = 0

        async def get(self, endpoint: str) -> dict:
            self.polls += 1
            if self.polls == 1:
                return {"is_system_auditor": False}
            return {"is_system_auditor": True}

    summary = await assign_auditor_roles(
        FakeTargetClient(),
        [{"username": "alice", "source_id": 1, "target_id": 10}],
        role_definition_id=42,
    )
    assert summary.assigned_count == 1
    assert summary.verified_count == 1
    assert summary.failed == []


@pytest.mark.asyncio
async def test_preflight_gateway_access_raises_when_role_missing() -> None:
    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": []}

    class FakeHTTPClient:
        async def get(self, endpoint, params=None, headers=None):
            return FakeResponse()

    client = type(
        "Client",
        (),
        {
            "base_url": "https://aap.example.com/api/controller/v2",
            "token": "secret",
            "client": FakeHTTPClient(),
        },
    )()

    with pytest.raises(RuntimeError, match="Platform Auditor role_definition not found"):
        await preflight_gateway_access(client)


@pytest.mark.asyncio
async def test_assign_auditor_roles_records_gateway_post_failure() -> None:
    class FakeHTTPClient:
        async def post(self, endpoint, json=None, headers=None):
            raise RuntimeError("gateway down")

    class FakeTargetClient:
        def __init__(self) -> None:
            self.base_url = "https://aap.example.com/api/controller/v2"
            self.token = "secret"
            self.client = FakeHTTPClient()

    summary = await assign_auditor_roles(
        FakeTargetClient(),
        [{"username": "carol", "source_id": 3, "target_id": 12}],
        role_definition_id=42,
    )
    assert summary.assigned_count == 0
    assert len(summary.failed) == 1
    assert "RuntimeError" in summary.failed[0].error or ""


@pytest.mark.asyncio
async def test_assign_auditor_roles_records_sync_timeout() -> None:
    class FakeGWResponse:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": 901}

    class FailingSyncHTTPClient:
        async def post(self, endpoint, json=None, headers=None):
            return FakeGWResponse()

    class FakeTargetClient:
        def __init__(self) -> None:
            self.base_url = "https://aap.example.com/api/controller/v2"
            self.token = "secret"
            self.client = FailingSyncHTTPClient()

        async def get(self, endpoint: str) -> dict:
            return {"is_system_auditor": False}

    summary = await assign_auditor_roles(
        FakeTargetClient(),
        [{"username": "bob", "source_id": 2, "target_id": 11}],
        role_definition_id=42,
    )
    assert summary.assigned_count == 1
    assert summary.verified_count == 0
    assert len(summary.failed) == 1
    assert "did not sync" in summary.failed[0].error or ""


def test_auditor_dataclasses_defaults() -> None:
    result = AuditorAssignmentResult(
        username="x",
        source_id=1,
        target_id=2,
        success=True,
    )
    assert result.gateway_assignment_id is None
    summary = AuditorRolesSummary()
    assert summary.auditor_count == 0
