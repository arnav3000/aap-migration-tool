"""Bootstrap id_mappings by scanning source and target before import.

State stores the source→target ID graph required for FK remapping. Target
existence checks alone are not enough; this module seeds that graph up front
so migrate does not rediscover every object one HTTP call at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aap_migration.migration.state import MigrationState
from aap_migration.resources import (
    ORGANIZATION_SCOPED_RESOURCES,
    PARENT_SCOPED_RESOURCES,
    RESOURCE_REGISTRY,
    get_endpoint,
)
from aap_migration.utils.logging import get_logger
from aap_migration.utils.naming import should_apply_name_prefix

logger = get_logger(__name__)

# Types with no listable endpoint / no stable name key for a bulk scan.
_SKIP_BOOTSTRAP_TYPES = frozenset(
    {
        "host_inventory_memberships",
        "settings",
    }
)

_IDENTIFIER_FIELDS: dict[str, str] = {
    "users": "username",
    "instances": "hostname",
}


@dataclass
class BootstrapStats:
    """Result of a target bootstrap scan for one resource type."""

    mapped: int = 0
    unmatched: int = 0
    skipped: int = 0
    mapped_source_ids: list[int] = field(default_factory=list)


def _identifier_field(resource_type: str) -> str:
    return _IDENTIFIER_FIELDS.get(resource_type, "name")


def _expected_target_name(
    resource_type: str,
    source_item: dict[str, Any],
    name_prefix: str,
) -> str | None:
    field = _identifier_field(resource_type)
    raw = source_item.get(field)
    if not isinstance(raw, str) or not raw:
        return None
    if not name_prefix or not should_apply_name_prefix(resource_type, source_item):
        return raw
    if field != "name":
        # username / hostname are not prefixed today
        return raw
    return f"{name_prefix}{raw}"


def _source_match_key(
    resource_type: str,
    source_item: dict[str, Any],
    expected_name: str,
    state: MigrationState,
) -> Any | None:
    """Build a natural key using *target* scope IDs so it lines up with the target index."""
    if resource_type == "credentials":
        org_src = source_item.get("organization")
        cred_type_src = source_item.get("credential_type")
        org_tgt = (
            state.get_mapped_id("organizations", int(org_src)) if org_src is not None else None
        )
        type_tgt = (
            state.get_mapped_id("credential_types", int(cred_type_src))
            if cred_type_src is not None
            else None
        )
        if org_tgt is None or type_tgt is None:
            return None
        return (expected_name, int(org_tgt), int(type_tgt))

    if resource_type in ORGANIZATION_SCOPED_RESOURCES:
        org_src = source_item.get("organization")
        if org_src is None:
            return (expected_name, None)
        org_tgt = state.get_mapped_id("organizations", int(org_src))
        if org_tgt is None:
            return None
        return (expected_name, int(org_tgt))

    if resource_type in PARENT_SCOPED_RESOURCES:
        parent_field = PARENT_SCOPED_RESOURCES[resource_type]
        parent_src = source_item.get(parent_field)
        if parent_src is None:
            return None
        parent_type = {
            "inventory": "inventories",
            "unified_job_template": "job_templates",
        }.get(parent_field, f"{parent_field}s")
        parent_tgt = state.get_mapped_id(parent_type, int(parent_src))
        if parent_tgt is None and parent_field == "unified_job_template":
            parent_tgt = state.get_mapped_id("workflow_job_templates", int(parent_src))
        if parent_tgt is None:
            return None
        return (expected_name, int(parent_tgt))

    return expected_name


def _target_index_key(resource_type: str, item: dict[str, Any]) -> Any | None:
    field = _identifier_field(resource_type)
    name = item.get(field)
    if not isinstance(name, str) or not name:
        return None

    if resource_type == "credentials":
        org = item.get("organization")
        cred_type = item.get("credential_type")
        if org is None or cred_type is None:
            return None
        return (name, int(org), int(cred_type))

    if resource_type in ORGANIZATION_SCOPED_RESOURCES:
        org = item.get("organization")
        return (name, int(org) if org is not None else None)

    if resource_type in PARENT_SCOPED_RESOURCES:
        parent_field = PARENT_SCOPED_RESOURCES[resource_type]
        parent_id = item.get(parent_field)
        if parent_id is None:
            return None
        return (name, int(parent_id))

    return name


async def _list_resources(client: Any, resource_type: str) -> list[dict[str, Any]]:
    endpoint = get_endpoint(resource_type)
    if not endpoint:
        return []

    if hasattr(client, "list_resources"):
        result = await client.list_resources(resource_type, page_size=200)
        return list(result) if result else []

    if hasattr(client, "get_paginated"):
        result = await client.get_paginated(endpoint, page_size=200)
        return list(result) if result else []

    raise TypeError(f"Client cannot list {resource_type}: missing list_resources/get_paginated")


async def bootstrap_mappings_for_type(
    resource_type: str,
    source_client: Any,
    target_client: Any,
    state: MigrationState,
    *,
    name_prefix: str = "",
    org_ids: list[int] | None = None,
) -> BootstrapStats:
    """Scan source and target; seed id_mappings for objects that already exist.

    Matching uses natural keys (name / username / hostname, plus org or parent
    scope when required). Source org/parent IDs are remapped through existing
    state mappings so keys align with target IDs.
    """
    stats = BootstrapStats()
    if resource_type in _SKIP_BOOTSTRAP_TYPES:
        stats.skipped = 1
        return stats

    info = RESOURCE_REGISTRY.get(resource_type)
    if info is None or not info.endpoint:
        stats.skipped = 1
        return stats

    try:
        source_items = await _list_resources(source_client, resource_type)
        target_items = await _list_resources(target_client, resource_type)
    except Exception as exc:
        logger.warning(
            "target_bootstrap_list_failed",
            resource_type=resource_type,
            error=str(exc),
        )
        stats.skipped = 1
        return stats

    if org_ids and resource_type == "organizations":
        source_items = [i for i in source_items if i.get("id") in org_ids]
    elif org_ids and resource_type not in (
        "settings",
        "instances",
        "instance_groups",
        "credential_types",
        "users",
        "system_job_templates",
    ):
        filtered = []
        for item in source_items:
            org = item.get("organization")
            sf_org = (item.get("summary_fields") or {}).get("organization", {}).get("id")
            if org in org_ids or sf_org in org_ids:
                filtered.append(item)
            elif resource_type in ("credentials", "execution_environments", "applications") and (
                org is None and sf_org is None
            ):
                filtered.append(item)
        source_items = filtered

    target_by_key: dict[Any, dict[str, Any]] = {}
    for item in target_items:
        key = _target_index_key(resource_type, item)
        if key is not None and key not in target_by_key:
            target_by_key[key] = item

    for source_item in source_items:
        source_id = source_item.get("id")
        if source_id is None:
            continue
        try:
            source_id_int = int(source_id)
        except (TypeError, ValueError):
            continue

        if state.is_migrated(resource_type, source_id_int) and state.get_mapped_id(
            resource_type, source_id_int
        ):
            stats.mapped += 1
            stats.mapped_source_ids.append(source_id_int)
            continue

        expected_name = _expected_target_name(resource_type, source_item, name_prefix)
        if expected_name is None:
            stats.unmatched += 1
            continue

        match_key = _source_match_key(resource_type, source_item, expected_name, state)
        if match_key is None:
            stats.unmatched += 1
            continue

        existing = target_by_key.get(match_key)
        if not existing or existing.get("id") is None:
            stats.unmatched += 1
            continue

        target_id = int(existing["id"])
        source_name = source_item.get(_identifier_field(resource_type)) or expected_name
        target_name = existing.get(_identifier_field(resource_type)) or expected_name
        try:
            state.save_id_mapping(
                resource_type=resource_type,
                source_id=source_id_int,
                target_id=target_id,
                source_name=str(source_name) if source_name else None,
                target_name=str(target_name) if target_name else None,
            )
            state.mark_completed(
                resource_type=resource_type,
                source_id=source_id_int,
                target_id=target_id,
                target_name=str(target_name) if target_name else None,
                source_name=str(source_name) if source_name else expected_name,
            )
            stats.mapped += 1
            stats.mapped_source_ids.append(source_id_int)
        except Exception as exc:
            logger.debug(
                "target_bootstrap_map_failed",
                resource_type=resource_type,
                source_id=source_id_int,
                error=str(exc),
            )
            stats.unmatched += 1

    logger.info(
        "target_bootstrap_complete",
        resource_type=resource_type,
        mapped=stats.mapped,
        unmatched=stats.unmatched,
        target_indexed=len(target_by_key),
        source_count=len(source_items),
    )
    return stats
