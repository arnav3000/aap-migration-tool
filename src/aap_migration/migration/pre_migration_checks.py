"""Optional pre-migration checks (credential compare, schema compare).

Injected at CLI entry points; coordinator core migration does not require these.
"""

from __future__ import annotations

from typing import Any

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.migration.credential_comparator import CredentialComparator
from aap_migration.migration.state import MigrationState
from aap_migration.schema.comparator import SchemaComparator
from aap_migration.schema.models import ComparisonResult, Severity
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


async def compare_and_verify_credentials(
    source_client: AAPSourceClient,
    target_client: AAPTargetClient,
    state: MigrationState,
    report_path: str | None = None,
) -> dict[str, Any]:
    """Compare credentials between source and target before migration."""
    logger.info("credential_comparison_starting")

    comparator = CredentialComparator(
        source_client=source_client,
        target_client=target_client,
        state=state,
    )
    result = await comparator.compare_credentials()
    report = comparator.generate_report(result)

    if report_path:
        try:
            import os

            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, "w") as f:
                f.write(report)
            logger.info("credential_comparison_report_saved", path=report_path)
        except Exception as exc:
            logger.error("credential_report_save_failed", path=report_path, error=str(exc))

    summary = {
        "total_source": result.total_source,
        "total_target": result.total_target,
        "matching_count": result.matching_credentials,
        "missing_count": len(result.missing_in_target),
        "managed_skipped": result.managed_credentials_skipped,
        "missing_credentials": [
            {
                "source_id": diff.source_id,
                "name": diff.name,
                "type": diff.credential_type_name,
                "organization": diff.organization_name,
            }
            for diff in result.missing_in_target
        ],
        "report": report,
    }

    logger.info(
        "credential_comparison_completed",
        total_source=result.total_source,
        total_target=result.total_target,
        missing=len(result.missing_in_target),
    )
    return summary


async def compare_schemas_before_migration(
    source_client: AAPSourceClient,
    target_client: AAPTargetClient,
    resource_types: list[str],
) -> dict[str, ComparisonResult]:
    """Compare source and target schemas for the given resource types."""
    schema_comparator = SchemaComparator()
    comparisons: dict[str, ComparisonResult] = {}

    logger.info(
        "schema_comparison_started",
        resource_types=resource_types,
        count=len(resource_types),
    )

    for resource_type in resource_types:
        try:
            source_schema = await schema_comparator.fetch_schema(source_client, resource_type)
            target_schema = await schema_comparator.fetch_schema(target_client, resource_type)
            comparison = schema_comparator.compare_schemas(
                resource_type, source_schema, target_schema
            )
            comparisons[resource_type] = comparison
            if comparison.has_breaking_changes:
                logger.warning(
                    "schema_breaking_changes_detected",
                    resource_type=resource_type,
                    breaking_changes_count=sum(
                        1 for diff in comparison.field_diffs if diff.is_breaking
                    ),
                )
        except Exception as exc:
            logger.error(
                "schema_comparison_failed",
                resource_type=resource_type,
                error=str(exc),
            )

    logger.info(
        "schema_comparison_completed",
        total_resource_types=len(resource_types),
        comparisons_count=len(comparisons),
        breaking_changes_count=sum(1 for c in comparisons.values() if c.has_breaking_changes),
    )
    return comparisons


def has_critical_schema_issues(comparisons: dict[str, ComparisonResult]) -> bool:
    """Return True if any comparison has critical severity issues."""
    for comparison in comparisons.values():
        for diff in comparison.field_diffs:
            if diff.severity == Severity.CRITICAL:
                return True
        for change in comparison.schema_changes:
            if change.severity == Severity.CRITICAL:
                return True
    return False
