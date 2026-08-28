"""Helpers for built-in / managed credential type ID mapping."""

from __future__ import annotations

from typing import Any

from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)

BUILTIN_CREDENTIAL_TYPE_MAX_ID = 27


def is_builtin_credential_type_id(credential_type_id: Any) -> bool:
    try:
        return int(credential_type_id) <= BUILTIN_CREDENTIAL_TYPE_MAX_ID  # noqa: SLF001
    except (TypeError, ValueError):
        return False


async def map_managed_credential_types(
    source_client: Any, target_client: Any, state: MigrationState
) -> int:
    """Create ID mappings for managed credential types (best-effort stub for API)."""
    try:
        source_resp = await source_client.get(
            "credential_types/", params={"managed": "true", "page_size": 200}
        )
        source_types = source_resp.get("results", []) if isinstance(source_resp, dict) else []
        target_resp = await target_client.get(
            "credential_types/", params={"managed": "true", "page_size": 200}
        )
        target_types = target_resp.get("results", []) if isinstance(target_resp, dict) else []
        target_by_name = {t["name"]: t["id"] for t in target_types if "name" in t and "id" in t}
        mapped = 0
        for st in source_types:
            name = st.get("name")
            sid = st.get("id")
            tid = target_by_name.get(name)
            if tid is not None:
                try:
                    state.create_or_update_mapping(
                        resource_type="credential_types",
                        source_id=sid,
                        target_id=tid,
                        source_name=name,
                    )
                    mapped += 1
                except Exception:
                    pass
        return mapped
    except Exception as e:
        logger.debug("map_managed_credential_types_failed", error=str(e))
        return 0
