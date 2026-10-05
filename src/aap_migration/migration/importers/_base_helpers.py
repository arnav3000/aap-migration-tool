"""Base helpers split from importers.base (constants + stateless lookups).

Kept as a separate module (single consumer: ``base.ResourceImporter``) to
isolate DB-backed lookups and stats accessors from the 965-line base
importer and to keep the ``base -> _base_helpers -> _registry`` import
direction explicit. Folding it back would save no lines from base; a
meaningful future split is pure stats vs DB lookups, not this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aap_migration.utils.logging import get_logger

if TYPE_CHECKING:
    from aap_migration.client.aap_target_client import AAPTargetClient
    from aap_migration.migration.state import MigrationState

logger = get_logger(__name__)

__all__ = [
    "BaseLookupMixin",
]


class BaseLookupMixin:
    """Stateless lookups + stats accessors (mixin for ResourceImporter)."""

    client: AAPTargetClient
    state: MigrationState
    stats: dict[str, int]
    import_errors: list[dict[str, Any]]
    DEPENDENCIES: dict[str, str] = {}

    def _infer_resource_type_from_field(self, field_name: str) -> str | None:
        # Fallback only for error-message enrichment when a field is not in
        # the leaf DEPENDENCIES map (the registry remains the source of truth
        # for closure and resolution). Covers known FK fields; unknown
        # fields return None and the caller falls back to ID-only text.
        field_to_resource_type = {
            "credential": "credentials",
            "credential_type": "credential_types",
            "default_environment": "execution_environments",
            "execution_environment": "execution_environments",
            "group_id": "inventory_groups",
            "host_id": "hosts",
            "instance_group": "instance_groups",
            "inventory": "inventories",
            "inventory_id": "inventories",
            "job_template": "job_templates",
            "organization": "organizations",
            "parent": "inventory_groups",
            "project": "projects",
            "source_credential": "credentials",
            "source_project": "projects",
            "team": "teams",
            "unified_job_template": "job_templates",
            "user": "users",
            "webhook_credential": "credentials",
            "workflow_job_template": "workflow_job_templates",
        }
        return field_to_resource_type.get(field_name)

    def _get_dependency_name(self, resource_type: str, source_id: int) -> str | None:
        """Get the source name of a dependency resource from the database.

        Read-only lookup used only to enrich API error messages with source
        names (see ``base._enrich_api_error_message``). Results are cached
        per importer instance so repeated invalid-PK errors for the same
        dependency do not fan out to N read transactions. A missing row or
        DB failure returns None and the caller falls back to ID-only text.

        Args:
            resource_type: Type of dependency resource
            source_id: Source ID of the dependency

        Returns:
            Source resource name or None if not found
        """
        cache: dict[tuple[str, int], str | None] = (
            getattr(self, "_dependency_name_cache", None) or {}
        )
        self._dependency_name_cache = cache
        cache_key = (resource_type, source_id)
        if cache_key in cache:
            return cache[cache_key]
        try:
            from aap_migration.migration.database import get_session
            from aap_migration.migration.models import MigrationProgress

            with get_session(self.state.database_url) as session:
                progress = (
                    session.query(MigrationProgress)
                    .filter_by(resource_type=resource_type, source_id=source_id)
                    .first()
                )

                if progress and progress.source_name:
                    name = str(progress.source_name)
                    cache[cache_key] = name
                    return name

        except Exception as e:
            logger.debug(
                "dependency_name_lookup_failed",
                resource_type=resource_type,
                source_id=source_id,
                error=str(e),
            )

        cache[cache_key] = None
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
