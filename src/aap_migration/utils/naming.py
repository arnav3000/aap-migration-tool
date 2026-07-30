"""Helpers for applying source name prefixes during migration."""

from __future__ import annotations

from typing import Any

# Resources that never receive a name prefix.
_NAME_PREFIX_SKIP_TYPES = frozenset({"users", "settings", "host_inventory_memberships"})


def should_apply_name_prefix(resource_type: str, resource: dict[str, Any] | None = None) -> bool:
    """Return True when ``resource_type`` (and optional resource) should be prefixed."""
    if resource_type in _NAME_PREFIX_SKIP_TYPES:
        return False
    if resource is not None and resource_type in ("credential_types", "credentials"):
        if resource.get("managed"):
            return False
    return True


def apply_name_prefix(resource_type: str, resource: dict[str, Any], name_prefix: str) -> None:
    """Prepend ``name_prefix`` to ``resource["name"]`` when appropriate.

    Built-in (``managed=True``) credential types and managed credentials keep their
    canonical AAP names so they can be matched on the target. Custom credential
    types and all other named resources are prefixed.

    Always records ``_name_prefix`` on the resource when a prefix is configured so
    import can resolve related FKs by prefixed name when ID mappings are missing.
    """
    if not name_prefix:
        return
    resource["_name_prefix"] = name_prefix
    if "name" not in resource:
        return
    if not should_apply_name_prefix(resource_type, resource):
        return
    resource["name"] = f"{name_prefix}{resource['name']}"
