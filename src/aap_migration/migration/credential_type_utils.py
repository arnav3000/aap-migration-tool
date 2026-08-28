"""Helpers for built-in / managed credential type ID mapping."""

from __future__ import annotations

from typing import Any

from aap_migration.migration.state import MigrationState
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)

# Built-in credential type IDs are managed by AAP and historically land in this
# range. Custom types start above it. Prefer name-based mapping when possible;
# this ceiling is the fallback when no mapping exists yet.
BUILTIN_CREDENTIAL_TYPE_MAX_ID = 27


def is_builtin_credential_type_id(credential_type_id: Any) -> bool:
    """Return True when ``credential_type_id`` looks like a built-in type ID."""
    try:
        return int(credential_type_id) <= BUILTIN_CREDENTIAL_TYPE_MAX_ID
    except (TypeError, ValueError):
        return False


async def map_managed_credential_types(
    source_client: Any,
    target_client: Any,
    state: MigrationState,
) -> int:
    """Create ID mappings for managed (built-in) credential types by name.

    Managed types (Machine, Source Control, Vault, etc.) exist on both source
    and target but may have different IDs. Match by name and store mappings so
    credential transform/import can resolve ``credential_type`` FKs.
    """
    try:
        source_types_response = await source_client.get(
            "credential_types/", params={"managed": "true", "page_size": 200}
        )
        source_types = source_types_response.get("results", [])

        target_types_response = await target_client.get(
            "credential_types/", params={"managed": "true", "page_size": 200}
        )
        target_types = target_types_response.get("results", [])

        target_by_name = {t["name"]: t["id"] for t in target_types}

        mapped_count = 0
        for source_type in source_types:
            source_name = source_type["name"]
            source_id = source_type["id"]
            target_id = target_by_name.get(source_name)

            if target_id:
                state.create_or_update_mapping(
                    resource_type="credential_types",
                    source_id=source_id,
                    target_id=target_id,
                    source_name=source_name,
                )
                mapped_count += 1
                logger.debug(
                    "mapped_managed_credential_type",
                    name=source_name,
                    source_id=source_id,
                    target_id=target_id,
                )
            else:
                logger.warning(
                    "managed_credential_type_missing_on_target",
                    name=source_name,
                    source_id=source_id,
                )

        return mapped_count

    except Exception as e:
        logger.error("failed_to_map_managed_credential_types", error=str(e))
        return 0
