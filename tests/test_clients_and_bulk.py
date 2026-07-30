from __future__ import annotations

import asyncio

import pytest

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.client.bulk_operations import BulkOperations
from aap_migration.client.exceptions import BulkOperationError, ConflictError
from aap_migration.config import AAPInstanceConfig, PerformanceConfig


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def make_source_client() -> AAPSourceClient:
    return AAPSourceClient(AAPInstanceConfig(url="https://source.example.com", token="token"))


def make_target_client(url: str = "https://target.example.com") -> AAPTargetClient:
    return AAPTargetClient(AAPInstanceConfig(url=url, token="token"))


@pytest.mark.asyncio
async def test_source_client_version_pagination_parallel_and_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_source_client()
    requests: list[tuple[str, dict | None]] = []

    async def fake_get(endpoint: str, params=None):
        requests.append((endpoint, params))
        if endpoint == "config/":
            return {"version": "2.4.1"}
        if endpoint == "organizations/":
            page = params["page"]
            if page == 1:
                return {"results": [{"id": 1}], "count": 3, "next": "/api/v2/organizations/?page=2"}
            if page == 2:
                return {"results": [{"id": 2}, {"id": 3}], "count": 3, "next": None}
        if endpoint == "users/":
            if "page" not in (params or {}):
                return {"count": 3}
            page = params["page"]
            return {
                "results": [{"id": page, "name": f"user-{page}"}],
                "count": 3,
                "next": None if page == 3 else f"/api/v2/users/?page={page + 1}",
            }
        return {"results": [], "count": 0, "next": None}

    async def fake_get_paginated(endpoint: str, params=None):
        return [{"endpoint": endpoint, "params": params}]

    monkeypatch.setattr(client, "get", fake_get)

    try:
        assert await client.get_version() == "2.4.1"
        assert await client.get_version() == "2.4.1"
        assert await client.get_paginated("organizations/") == [{"id": 1}, {"id": 2}, {"id": 3}]

        items = [
            item
            async for item in client.get_all_resources_parallel(
                "users/", page_size=1, max_concurrent=2
            )
        ]
        assert items == [
            {"id": 1, "name": "user-1"},
            {"id": 2, "name": "user-2"},
            {"id": 3, "name": "user-3"},
        ]

        monkeypatch.setattr(client, "get_paginated", fake_get_paginated)
        assert await client.get_organizations({"name": "Default"}) == [
            {"endpoint": "organizations/", "params": {"name": "Default"}}
        ]
        assert await client.get_hosts(7, {"name": "web"}) == [
            {"endpoint": "inventories/7/hosts/", "params": {"name": "web"}}
        ]
        assert await client.get_groups(None, {"search": "db"}) == [
            {"endpoint": "groups/", "params": {"search": "db"}}
        ]
        assert await client.get_workflow_nodes(8, {"page_size": 50}) == [
            {"endpoint": "workflow_job_templates/8/workflow_nodes/", "params": {"page_size": 50}}
        ]
        assert await client.get_job_template_credentials(9) == [
            {"endpoint": "job_templates/9/credentials/", "params": None}
        ]
        assert await client.search_resources("inventories", "prod", {"page_size": 10}) == [
            {"endpoint": "inventories/", "params": {"page_size": 10, "search": "prod"}}
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_source_client_version_fallback_and_count(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_source_client()

    async def fallback_get(endpoint: str, params=None):
        if endpoint == "config/":
            return {"ansible_version": "2.4.9"}
        return {"count": 12}

    monkeypatch.setattr(client, "get", fallback_get)

    try:
        assert await client.get_version() == "2.4.9"
        assert await client.get_count("hosts/") == 12
    finally:
        await client.close()

    error_client = make_source_client()

    async def broken_get(endpoint: str, params=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(error_client, "get", broken_get)

    try:
        assert await error_client.get_version() == "2.4.0"
    finally:
        await error_client.close()


@pytest.mark.asyncio
async def test_target_client_core_crud_listing_cancel_and_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_target_client()
    assert client.base_url.endswith("/api/controller/v2")

    async def fake_get(endpoint: str, params=None):
        if endpoint == "config/":
            return {"version": "2.6.1"}
        if endpoint == "inventories/5/":
            return {"id": 5}
        if endpoint == "hosts/9/":
            raise RuntimeError("missing")
        if endpoint == "jobs/3/":
            return {"id": 3, "status": "running", "can_cancel": True}
        if endpoint == "jobs/4/":
            return {"id": 4, "status": "successful", "can_cancel": False}
        if endpoint == "jobs/5/":
            return {"id": 5, "status": "canceling", "can_cancel": True}
        if endpoint == "jobs/6/":
            return {"id": 6, "status": "running", "can_cancel": False}
        if endpoint == "groups/":
            return {"results": [{"id": 1}], "next": None}
        if endpoint == "inventory_sources/":
            page = int((params or {}).get("page", 1))
            if page == 1:
                return {
                    "results": [{"id": 1}],
                    "next": "https://target.example.com/api/controller/v2/inventory_sources/?page=2&page_size=1",
                }
            return {"results": [{"id": 2}], "next": None}
        if endpoint == "ping/":
            return {"ok": True}
        return {"results": [], "count": 0, "next": None}

    async def fake_post(endpoint: str, json_data=None):
        if endpoint == "organizations/":
            return {"id": 11, "name": json_data["name"]}
        if endpoint == "job_templates/12/launch/":
            return {"job": 99, "extra_vars": json_data}
        if endpoint.endswith("/cancel/"):
            return {"status": "canceling"}
        return {"id": 1}

    async def fake_patch(endpoint: str, json_data=None):
        return {"endpoint": endpoint, "data": json_data}

    async def fake_delete(endpoint: str):
        return {"deleted": endpoint}

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(client, "post", fake_post)
    monkeypatch.setattr(client, "patch", fake_patch)
    monkeypatch.setattr(client, "delete", fake_delete)

    try:
        assert await client.get_version() == "2.6.1"
        assert await client.create_resource("organizations", {"name": "Default"}) == {
            "id": 11,
            "name": "Default",
        }
        assert await client.update_resource("inventory_groups", 5, {"name": "web"}) == {
            "endpoint": "groups/5/",
            "data": {"name": "web"},
        }
        assert await client.delete_resource("inventories", 5) == {"deleted": "inventories/5/"}
        assert await client.get_resource("inventories", 5) == {"id": 5}
        assert await client.resource_exists("inventories", 5) is True
        assert await client.resource_exists("hosts", 9) is False

        async def get_for_find(endpoint: str, params=None):
            return {"results": [{"id": 33, "name": params["name"]}]}

        monkeypatch.setattr(client, "get", get_for_find)
        assert await client.find_resource_by_name(
            "credentials",
            "Vault",
            organization="Default",
            parent_id=10,
            parent_field="inventory",
        ) == {"id": 33, "name": "Vault"}

        monkeypatch.setattr(client, "get", fake_get)
        assert await client.create_organization({"name": "Default"}) == {
            "id": 11,
            "name": "Default",
        }
        assert await client.create_inventory({"name": "Inventory"}) == {"id": 1}
        assert await client.create_host(7, {"name": "host1"}) == {"id": 1}
        assert await client.create_credential({"name": "Cred"}) == {"id": 1}
        assert await client.create_project({"name": "Proj"}) == {"id": 1}
        assert await client.create_job_template({"name": "JT"}) == {"id": 1}
        assert await client.create_workflow_job_template({"name": "WJT"}) == {"id": 1}
        assert await client.launch_job_template(12, {"a": 1}) == {
            "job": 99,
            "extra_vars": {"extra_vars": {"a": 1}},
        }
        assert await client.get_job_status(3) == {"id": 3, "status": "running", "can_cancel": True}
        assert await client.cancel_job(3) == {"status": "canceling"}
        assert await client.cancel_job(4) == {"id": 4, "status": "successful", "can_cancel": False}
        assert await client.cancel_job(5) == {"id": 5, "status": "canceling", "can_cancel": True}
        assert await client.cancel_job(6) == {"id": 6, "status": "running", "can_cancel": False}
        assert await client.get_count("groups") == 0
        assert await client.list_resources("inventory_sources", page_size=1) == [
            {"id": 1},
            {"id": 2},
        ]
        assert await client.validate_connectivity() is True

        async def failing_ping(endpoint: str, params=None):
            raise RuntimeError("offline")

        monkeypatch.setattr(client, "get", failing_ping)
        assert await client.validate_connectivity() is False

        monkeypatch.setattr(
            client.client,
            "post",
            lambda endpoint, json=None, headers=None: asyncio.sleep(
                0, result=FakeHTTPResponse({"id": 7})
            ),
        )
        monkeypatch.setattr(
            client.client,
            "get",
            lambda endpoint, headers=None: asyncio.sleep(
                0, result=FakeHTTPResponse({"results": [{"id": 1}]})
            ),
        )
        assert await client.create_gateway_authenticator(
            "LDAP", "plugin", {"uri": "ldap://example"}
        ) == {"id": 7}
        assert await client.list_gateway_authenticators() == [{"id": 1}]
        assert await client.create_authenticator_map(
            7,
            "LDAP Map",
            "organization",
            {"groups": {"has_or": ["cn=eng"]}},
            organization="Engineering",
            role="Organization Member",
        ) == {"id": 7}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_target_client_conflict_and_cancel_race_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_target_client("https://target.example.com/api/controller/v2")

    async def raise_conflict(endpoint: str, json_data=None):
        raise ConflictError("exists", status_code=409)

    async def fake_find(
        resource_type: str,
        name: str,
        organization=None,
        organization_id=None,
        parent_id=None,
        parent_field=None,
    ):
        return {"id": 44, "name": name}

    monkeypatch.setattr(client, "post", raise_conflict)
    monkeypatch.setattr(client, "find_resource_by_name", fake_find)

    try:
        assert await client.create_resource("organizations", {"name": "Default"}) == {
            "id": 44,
            "name": "Default",
        }

        async def fake_get(endpoint: str, params=None):
            return {"id": 7, "status": "running", "can_cancel": True}

        async def not_allowed(endpoint: str, json_data=None):
            raise RuntimeError("cancel not allowed")

        monkeypatch.setattr(client, "get", fake_get)
        monkeypatch.setattr(client, "post", not_allowed)
        assert await client.cancel_job(7) == {"id": 7, "status": "running", "can_cancel": True}

        class MethodNotAllowed(Exception):
            status_code = 405

        async def post_405(endpoint: str, json_data=None):
            raise MethodNotAllowed("no cancel")

        monkeypatch.setattr(client, "post", post_405)
        with pytest.raises(MethodNotAllowed):
            await client.cancel_job(7)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bulk_operations_cover_success_and_failure_paths() -> None:
    class FakeClient:
        def __init__(self):
            self.posts = []
            self.gets = []

        async def post(self, endpoint, json_data=None, timeout=None):
            self.posts.append((endpoint, json_data, timeout))
            if endpoint == "bulk/host_create/":
                return {"hosts": json_data["hosts"], "failed": []}
            if endpoint == "bulk/job_launch/":
                return {"jobs": json_data["templates"], "failed": []}
            raise RuntimeError("unexpected")

        async def get(self, endpoint, params=None):
            self.gets.append((endpoint, params))
            return {"count": 3}

    fake_client = FakeClient()
    bulk = BulkOperations(fake_client, PerformanceConfig(bulk_operation_timeout=123.0))

    assert await bulk.bulk_create_hosts(7, [{"name": "a"}, {"name": "b"}], batch_size=500) == {
        "hosts": [{"name": "a"}, {"name": "b"}],
        "failed": [],
    }
    assert await bulk.bulk_create_hosts_batched(
        7, [{"name": "a"}, {"name": "b"}, {"name": "c"}], batch_size=2
    ) == [
        {"hosts": [{"name": "a"}, {"name": "b"}], "failed": []},
        {"hosts": [{"name": "c"}], "failed": []},
    ]
    assert await bulk.bulk_launch_jobs([1, 2], {"env": "dev"}) == {"jobs": [1, 2], "failed": []}
    assert BulkOperations.chunk_hosts([{"id": 1}, {"id": 2}, {"id": 3}], chunk_size=2) == [
        [{"id": 1}, {"id": 2}],
        [{"id": 3}],
    ]
    assert await bulk.validate_bulk_host_creation(7, 3) is True
    assert await bulk.get_bulk_operation_status("op-1") == {"count": 3}
    assert fake_client.posts[0][2] == 123.0

    async def delete_hosts(ids):
        return {"hosts": {host_id: {"deleted": True} for host_id in ids}}

    bulk._request_bulk_delete_hosts = delete_hosts  # type: ignore[method-assign]
    assert await bulk.bulk_delete_hosts([1, 2, 3], batch_size=2) == {
        "hosts": {1: {"deleted": True}, 2: {"deleted": True}}
    }

    progress: list[tuple[int, int]] = []

    async def maybe_fail(host_ids, batch_size=500):
        if host_ids == [3, 4]:
            raise BulkOperationError("boom", failed_items=host_ids)
        return {"hosts": {host_id: {} for host_id in host_ids}}

    bulk.bulk_delete_hosts = maybe_fail  # type: ignore[method-assign]
    assert await bulk.bulk_delete_hosts_batched(
        [1, 2, 3, 4],
        batch_size=2,
        progress_callback=lambda deleted, failed: progress.append((deleted, failed)),
    ) == {
        "total_requested": 4,
        "total_deleted": 2,
        "total_failed": 2,
    }
    assert progress == [(2, 0), (2, 2)]


@pytest.mark.asyncio
async def test_bulk_operations_wrap_errors() -> None:
    class BrokenClient:
        async def post(self, endpoint, json_data=None, timeout=None):
            raise RuntimeError("boom")

        async def get(self, endpoint, params=None):
            raise RuntimeError("boom")

    bulk = BulkOperations(BrokenClient())

    with pytest.raises(BulkOperationError, match="Bulk host creation failed"):
        await bulk.bulk_create_hosts(7, [{"name": "a"}])

    with pytest.raises(BulkOperationError, match="Bulk job launch failed"):
        await bulk.bulk_launch_jobs([1, 2])

    with pytest.raises(BulkOperationError, match="Bulk host deletion failed"):
        await bulk.bulk_delete_hosts([1, 2])

    assert await bulk.validate_bulk_host_creation(7, 1) is False
