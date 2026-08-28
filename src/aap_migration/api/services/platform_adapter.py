"""Generic platform adapter for AAP resources (Task 4 clean)."""

from __future__ import annotations

from typing import Any

from aap_migration.api.services.engine_adapter import connection_to_aap_config
from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.resources import RESOURCE_REGISTRY


class PlatformAdapter:
    """Thin adapter around AAPSourceClient for generic resource listing."""

    def __init__(self, connection) -> None:  # Connection model
        cfg = connection_to_aap_config(connection)
        self.client = AAPSourceClient(config=cfg)
        self.connection = connection

    async def discover_resource_types(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rtype, info in RESOURCE_REGISTRY.items():
            out.append(
                {
                    "name": rtype,
                    "endpoint": info.endpoint,
                    "description": info.description,
                    "migration_order": info.migration_order,
                    "cleanup_order": info.cleanup_order,
                    "has_exporter": info.has_exporter,
                    "has_importer": info.has_importer,
                }
            )
        out.sort(key=lambda x: x["migration_order"])
        return out

    async def fetch_all(self, resource_type: str) -> list[dict[str, Any]]:
        # Use get_paginated via the underlying client's generic method
        endpoint = (
            RESOURCE_REGISTRY[resource_type].endpoint
            if resource_type in RESOURCE_REGISTRY
            else f"{resource_type}/"
        )
        return await self.client.get_paginated(endpoint)

    async def list_resources(
        self, resource_type: str, page: int = 1, page_size: int = 50, search: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        endpoint = (
            RESOURCE_REGISTRY[resource_type].endpoint
            if resource_type in RESOURCE_REGISTRY
            else f"{resource_type}/"
        )
        resp = await self.client.get(endpoint, params=params)
        return {
            "count": resp.get("count", 0),
            "next": resp.get("next"),
            "previous": resp.get("previous"),
            "results": resp.get("results", []),
        }
