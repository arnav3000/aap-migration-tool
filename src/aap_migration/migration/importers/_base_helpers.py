"""Base helpers split from importers.base (constants + stateless lookups)."""

from __future__ import annotations

from typing import Any

# Canonical scope sets live in _registry.RESOURCE_SPECS (single source of
# truth); re-exported here so existing ``from _base_helpers import ...``
# paths keep working without forking the vocabulary.
from aap_migration.migration.importers._registry import (
    ORGANIZATION_REQUIRED_RESOURCES,
    ORGANIZATION_SCOPED_RESOURCES,
)

__all__ = [
    "ORGANIZATION_REQUIRED_RESOURCES",
    "ORGANIZATION_SCOPED_RESOURCES",
    "BaseLookupMixin",
]


class BaseLookupMixin:
    """Stateless lookups + stats accessors (mixin for ResourceImporter)."""

    state: Any
    stats: dict[str, int]
    import_errors: list[dict[str, Any]]
    DEPENDENCIES: dict[str, str] = {}

    def _infer_resource_type_from_field(self, field_name: str) -> str | None:
        field_to_resource_type = {
            "inventory": "inventories",
            "project": "projects",
            "organization": "organizations",
            "credential": "credentials",
            "webhook_credential": "credentials",
            "execution_environment": "execution_environments",
            "instance_group": "instance_groups",
            "job_template": "job_templates",
            "workflow_job_template": "workflow_job_templates",
            "unified_job_template": "job_templates",
        }
        return field_to_resource_type.get(field_name)

    def _get_dependency_name(self, resource_type: str, source_id: int) -> str | None:
        """Return the recorded source name via MigrationState (no direct DB use)."""
        try:
            get_source_name = getattr(self.state, "get_source_name", None)
            if callable(get_source_name):
                name = get_source_name(resource_type, source_id)
                if name is None:
                    return None
                assert isinstance(name, str)
                return name
        except Exception:
            pass
        return None

    def get_stats(self) -> dict[str, int]:
        return self.stats.copy()

    def reset_stats(self) -> None:
        self.stats = {
            "imported_count": 0,
            "error_count": 0,
            "conflict_count": 0,
            "skipped_count": 0,
        }

    def get_import_errors(self) -> list[dict[str, Any]]:
        return self.import_errors.copy()
