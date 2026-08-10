"""Shared ETL pipeline for per-resource-type migration.

Disk boundary (AGENTS.md invariant 5): CLI export/transform commands write to
``exports/`` and ``xformed/``. Coordinator and API routers that call this module
run export → transform → import **in memory** by design — they do not write
intermediate files. Use explicit disk phases only when orchestrating full CLI ETL.

Optional ``write_to_disk`` flags may be added for debugging; default remains
in-memory for web/API paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.client.exceptions import ExportStoppedEarlyError
from aap_migration.config import PerformanceConfig
from aap_migration.migration.exporter import create_exporter
from aap_migration.migration.importer import create_importer
from aap_migration.migration.state import MigrationState
from aap_migration.migration.target_bootstrap import bootstrap_mappings_for_type
from aap_migration.migration.transformer import SkipResourceError, create_transformer
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


@runtime_checkable
class ResourceExporterProtocol(Protocol):
    stats: dict[str, Any]

    def export(self) -> AsyncIterator[dict[str, Any]]: ...


@runtime_checkable
class ResourceTransformerProtocol(Protocol):
    def transform_resource(
        self,
        resource_type: str,
        data: dict[str, Any],
        validate: bool = True,
    ) -> dict[str, Any]: ...


@runtime_checkable
class ResourceImporterProtocol(Protocol):
    import_errors: list[dict[str, Any]]

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None: ...


@dataclass
class ETLStats:
    exported: int = 0
    transformed: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    skipped_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ETLComponents:
    exporter: ResourceExporterProtocol
    transformer: ResourceTransformerProtocol
    importer: ResourceImporterProtocol


async def bootstrap_resource_type(
    resource_type: str,
    source_client: AAPSourceClient,
    target_client: AAPTargetClient,
    state: MigrationState,
    *,
    name_prefix: str = "",
    org_ids: list[int] | None = None,
) -> Any:
    """Seed target ID mappings for a resource type before ETL."""
    return await bootstrap_mappings_for_type(
        resource_type,
        source_client,
        target_client,
        state,
        name_prefix=name_prefix,
        org_ids=org_ids,
    )


def create_etl_components(
    resource_type: str,
    source_client: AAPSourceClient,
    target_client: AAPTargetClient,
    state: MigrationState,
    performance_config: PerformanceConfig,
    *,
    dry_run: bool = False,
    resource_mappings: dict[str, dict[str, str]] | None = None,
    name_prefix: str = "",
    defer_project_sync: bool = False,
    has_transformer: bool = True,
) -> ETLComponents:
    """Create exporter, transformer, and importer for a resource type."""
    exporter = create_exporter(
        resource_type=resource_type,
        client=source_client,
        state=state,
        performance_config=performance_config,
    )
    transformer: ResourceTransformerProtocol | None
    if has_transformer:
        transformer = create_transformer(
            resource_type=resource_type,
            dry_run=dry_run,
            state=state,
            defer_project_sync=defer_project_sync,
        )
    else:
        transformer = _PassthroughTransformer()
    importer = create_importer(
        resource_type=resource_type,
        client=target_client,
        state=state,
        performance_config=performance_config,
        resource_mappings=resource_mappings,
        name_prefix=name_prefix,
    )
    return ETLComponents(
        exporter=cast(ResourceExporterProtocol, exporter),
        transformer=transformer,
        importer=importer,
    )


class _PassthroughTransformer:
    """No-op transformer for resource types without transform logic."""

    def transform_resource(
        self,
        resource_type: str,
        data: dict[str, Any],
        validate: bool = True,
    ) -> dict[str, Any]:
        return data


async def run_export_transform_loop(
    resource_type: str,
    components: ETLComponents,
    *,
    phase_name: str = "",
    on_exported: Callable[[], None] | None = None,
    on_transformed: Callable[[], None] | None = None,
    on_skipped: Callable[[SkipResourceError, dict[str, Any]], None] | None = None,
    on_transform_failed: Callable[[int, Exception], None] | None = None,
) -> tuple[list[dict[str, Any]], ETLStats]:
    """Export and transform resources; returns payloads ready for import."""
    stats = ETLStats()
    resources_to_import: list[dict[str, Any]] = []

    async for resource in components.exporter.export():
        stats.exported += 1
        if on_exported:
            on_exported()

        source_id = resource["id"]
        resource["_source_id"] = source_id

        try:
            transformed = components.transformer.transform_resource(
                resource_type=resource_type,
                data=resource,
                validate=True,
            )
            stats.transformed += 1
            resources_to_import.append(transformed)
            if on_transformed:
                on_transformed()
        except SkipResourceError as exc:
            stats.skipped += 1
            stats.skipped_items.append(
                {
                    "phase": phase_name,
                    "resource_type": resource_type,
                    "source_id": exc.source_id,
                    "name": resource.get("name", "unknown"),
                    "reason": str(exc),
                    "missing_dependency": exc.missing_dependency,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
            if on_skipped:
                on_skipped(exc, resource)
        except Exception as exc:
            stats.failed += 1
            if on_transform_failed:
                on_transform_failed(int(source_id), exc)

    if components.exporter.stats.get("export_stopped_early"):
        raise ExportStoppedEarlyError(
            f"Export for {resource_type} stopped early due to API errors. Re-run export to resume."
        )

    return resources_to_import, stats


async def run_import_loop(
    resource_type: str,
    components: ETLComponents,
    resources: list[dict[str, Any]],
    state: MigrationState,
    *,
    dry_run: bool = False,
    on_imported: Callable[[], None] | None = None,
    on_skipped: Callable[[], None] | None = None,
    on_failed: Callable[[], None] | None = None,
) -> ETLStats:
    """Import transformed resources and update stats."""
    stats = ETLStats()

    if dry_run:
        stats.imported = len(resources)
        return stats

    for resource in resources:
        source_id = resource.pop("_source_id", None)
        if not source_id:
            continue

        result = await components.importer.import_resource(
            resource_type=resource_type,
            source_id=source_id,
            data=resource,
        )

        if result:
            stats.imported += 1
            if on_imported:
                on_imported()
        elif state.is_migrated(resource_type, source_id):
            stats.skipped += 1
            if on_skipped:
                on_skipped()
        else:
            stats.failed += 1
            if on_failed:
                on_failed()

    return stats


async def run_resource_type_etl(
    resource_type: str,
    source_client: AAPSourceClient,
    target_client: AAPTargetClient,
    state: MigrationState,
    performance_config: PerformanceConfig,
    *,
    phase_name: str = "",
    dry_run: bool = False,
    resource_mappings: dict[str, dict[str, str]] | None = None,
    name_prefix: str = "",
    org_ids: list[int] | None = None,
    defer_project_sync: bool = False,
    has_transformer: bool = True,
    on_exported: Callable[[], None] | None = None,
    on_transformed: Callable[[], None] | None = None,
    on_imported: Callable[[], None] | None = None,
    on_skipped_import: Callable[[], None] | None = None,
    on_failed_import: Callable[[], None] | None = None,
    on_skipped_transform: Callable[[SkipResourceError, dict[str, Any]], None] | None = None,
    on_transform_failed: Callable[[int, Exception], None] | None = None,
    write_to_disk: bool = False,
) -> dict[str, int]:
    """Run in-memory export → transform → import for one resource type.

    ``write_to_disk`` is reserved for future debug tooling; API paths leave it False.
    """
    if write_to_disk:
        logger.warning(
            "write_to_disk_requested",
            resource_type=resource_type,
            message="Disk ETL not implemented in pipeline; use CLI export/transform commands",
        )

    bootstrap = await bootstrap_resource_type(
        resource_type,
        source_client,
        target_client,
        state,
        name_prefix=name_prefix,
        org_ids=org_ids,
    )
    if bootstrap.mapped:
        logger.info(
            "target_bootstrap_seeded",
            resource_type=resource_type,
            mapped=bootstrap.mapped,
            unmatched=bootstrap.unmatched,
        )

    components = create_etl_components(
        resource_type,
        source_client,
        target_client,
        state,
        performance_config,
        dry_run=dry_run,
        resource_mappings=resource_mappings,
        name_prefix=name_prefix,
        defer_project_sync=defer_project_sync,
        has_transformer=has_transformer,
    )

    resources, et_stats = await run_export_transform_loop(
        resource_type,
        components,
        phase_name=phase_name,
        on_exported=on_exported,
        on_transformed=on_transformed,
        on_skipped=on_skipped_transform,
        on_transform_failed=on_transform_failed,
    )

    import_stats = await run_import_loop(
        resource_type,
        components,
        resources,
        state,
        dry_run=dry_run,
        on_imported=on_imported,
        on_skipped=on_skipped_import,
        on_failed=on_failed_import,
    )

    return {
        "exported": et_stats.exported,
        "transformed": et_stats.transformed,
        "imported": import_stats.imported,
        "skipped": et_stats.skipped + import_stats.skipped,
        "failed": et_stats.failed + import_stats.failed,
    }


async def run_coordinator_resource_etl(
    coordinator: Any,
    resource_type: str,
    phase_config: dict[str, Any],
) -> dict[str, int]:
    """Coordinator adapter: standard ETL with progress hooks and bulk-host delegation."""
    if resource_type == "hosts" and phase_config.get("use_bulk"):
        components = create_etl_components(
            resource_type,
            coordinator.source_client,
            coordinator.target_client,
            coordinator.state,
            coordinator.config.performance,
            dry_run=coordinator.config.dry_run,
            resource_mappings=coordinator.config.resource_mappings,
        )
        return cast(
            dict[str, int],
            await coordinator._execute_bulk_host_migration(
                components.exporter,
                components.transformer,
                components.importer,
            ),
        )

    def on_exported() -> None:
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(exported=1)

    def on_transformed() -> None:
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(transformed=1)

    def on_skipped(exc: SkipResourceError, resource: dict[str, Any]) -> None:
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(skipped=1)
        coordinator.metrics["skipped_items"].append(
            {
                "phase": phase_config["name"],
                "resource_type": resource_type,
                "source_id": exc.source_id,
                "name": resource.get("name", "unknown"),
                "reason": str(exc),
                "missing_dependency": exc.missing_dependency,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def on_transform_failed(source_id: int, exc: Exception) -> None:
        logger.error(
            "transformation_failed",
            resource_type=resource_type,
            source_id=source_id,
            error=str(exc),
        )
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(failed=1)

    def on_imported() -> None:
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(imported=1)

    def on_import_skipped() -> None:
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(skipped=1)

    def on_import_failed() -> None:
        if coordinator.progress_tracker:
            coordinator.progress_tracker.update_resource(failed=1)

    stats_dict = await run_resource_type_etl(
        resource_type,
        coordinator.source_client,
        coordinator.target_client,
        coordinator.state,
        coordinator.config.performance,
        phase_name=phase_config.get("name", ""),
        dry_run=coordinator.config.dry_run,
        resource_mappings=coordinator.config.resource_mappings,
        on_exported=on_exported,
        on_transformed=on_transformed,
        on_imported=on_imported,
        on_skipped_import=on_import_skipped,
        on_failed_import=on_import_failed,
        on_skipped_transform=on_skipped,
        on_transform_failed=on_transform_failed,
    )

    if (
        coordinator.progress_display
        and coordinator._current_phase_id
        and stats_dict["exported"] > 0
    ):
        if coordinator._current_phase_id in coordinator.progress_display.phase_states:
            coordinator.progress_display.phase_states[
                coordinator._current_phase_id
            ].total_items = stats_dict["exported"]
        if coordinator._current_phase_id in coordinator.progress_display.phase_tasks:
            task_id = coordinator.progress_display.phase_tasks[coordinator._current_phase_id]
            coordinator.progress_display.phase_progress.update(
                task_id, total=stats_dict["exported"]
            )

    if coordinator.progress_display and coordinator._current_phase_id:
        coordinator.progress_display.update_phase(
            coordinator._current_phase_id,
            completed=stats_dict["imported"] + stats_dict["failed"],
            failed=stats_dict["failed"],
            skipped=stats_dict["skipped"],
        )

    components = create_etl_components(
        resource_type,
        coordinator.source_client,
        coordinator.target_client,
        coordinator.state,
        coordinator.config.performance,
        dry_run=coordinator.config.dry_run,
        resource_mappings=coordinator.config.resource_mappings,
    )
    if components.importer.import_errors:
        logger.warning(
            "import_errors_summary",
            resource_type=resource_type,
            error_count=len(components.importer.import_errors),
            errors=components.importer.import_errors[:10],
            message="See full error list in migration report",
        )
        coordinator.metrics["errors"].extend(
            [
                {"phase": phase_config["name"], "resource_type": resource_type, **error}
                for error in components.importer.import_errors
            ]
        )

    if stats_dict["skipped"] > 0:
        logger.warning(
            "resources_skipped_summary",
            resource_type=resource_type,
            skipped_count=stats_dict["skipped"],
            message=f"{stats_dict['skipped']} resources were skipped due to missing dependencies",
        )

    logger.info("etl_pipeline_completed", resource_type=resource_type, stats=stats_dict)
    return stats_dict
