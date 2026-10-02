"""Base helpers split from importers.base (constants + stateless lookups)."""

from __future__ import annotations

from typing import Any, cast

ORGANIZATION_SCOPED_RESOURCES = {
    "teams",
    "projects",
    "inventories",
    "credentials",
    "job_templates",
    "workflow_job_templates",
    "notification_templates",
    "execution_environments",
    "labels",
}

ORGANIZATION_REQUIRED_RESOURCES = {
    "teams",
    "projects",
    "inventories",
    "notification_templates",
}


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
        try:
            from aap_migration.migration.database import get_session
            from aap_migration.migration.models import MigrationProgress
            from aap_migration.utils.logging import get_logger

            _log = get_logger(__name__)
            session: Any = get_session(self.state.database_url)
            progress = (
                session.query(MigrationProgress)
                .filter_by(resource_type=resource_type, source_id=source_id)
                .first()
            )
            session.close()
            if progress and progress.source_name:
                return cast("str | None", progress.source_name)
        except Exception as e:
            try:
                from aap_migration.utils.logging import get_logger

                get_logger(__name__).debug(
                    "dependency_name_lookup_failed",
                    resource_type=resource_type,
                    source_id=source_id,
                    error=str(e),
                )
            except Exception:
                pass
        return None

    def _get_dependencies(self, resource_type: str) -> dict[str, str]:
        return self.DEPENDENCIES

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
