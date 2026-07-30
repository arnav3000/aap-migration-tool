"""Helpers for applying source name prefixes during migration."""

from __future__ import annotations

from typing import Any

# Resources that never receive a name prefix.
_NAME_PREFIX_SKIP_TYPES = frozenset({"users", "settings", "host_inventory_memberships"})


def apply_name_prefix(resource_type: str, resource: dict[str, Any], name_prefix: str) -> None:
    """Prepend ``name_prefix`` to ``resource["name"]`` when appropriate.

    Built-in (``managed=True``) credential types keep their canonical AAP names so
    they can be matched on the target. Custom credential types and all other named
    resources are prefixed.
    """
    if not name_prefix or "name" not in resource:
        return
    if resource_type in _NAME_PREFIX_SKIP_TYPES:
        return
    if resource_type == "credential_types" and resource.get("managed"):
        return
    resource["name"] = f"{name_prefix}{resource['name']}"
