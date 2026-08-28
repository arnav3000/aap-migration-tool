"""Helpers for applying source name prefixes during migration."""

from __future__ import annotations

from typing import Any

_NAME_PREFIX_SKIP_TYPES = frozenset({"users", "settings", "host_inventory_memberships"})


def should_apply_name_prefix(resource_type: str, resource: dict[str, Any] | None = None) -> bool:
    if resource_type in _NAME_PREFIX_SKIP_TYPES:
        return False
    if resource is None:
        return True
    # Skip if resource is managed/builtin etc — minimal logic
    if resource.get("managed") is True:
        return False
    return True


def apply_name_prefix(resource_type: str, resource: dict[str, Any], prefix: str) -> dict[str, Any]:
    if not prefix:
        return resource
    if not should_apply_name_prefix(resource_type, resource):
        return resource
    name = resource.get("name")
    if isinstance(name, str) and name:
        resource = dict(resource)
        resource["name"] = f"{prefix}{name}"
    return resource
