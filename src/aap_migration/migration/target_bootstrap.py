"""Bootstrap id_mappings by scanning target before import (minimal stub for API)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aap_migration.migration.state import MigrationState
from aap_migration.resources import RESOURCE_REGISTRY
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)

_SKIP_BOOTSTRAP_TYPES = {"jobs", "workflow_jobs", "ad_hoc_commands"}
_IDENTIFIER_FIELDS = {"users": "username", "instances": "hostname"}


@dataclass
class BootstrapStats:
    """Result of a target bootstrap scan for one resource type."""

    mapped: int = 0
    unmatched: int = 0
    skipped: int = 0
    mapped_source_ids: list[int] = field(default_factory=list)


def _identifier_field(resource_type: str) -> str:
    return _IDENTIFIER_FIELDS.get(resource_type, "name")


async def bootstrap_mappings_for_type(
    resource_type: str,
    target_client: Any,
    state: MigrationState,
    name_prefix: str = "",
    org_ids: Any | None = None,
) -> BootstrapStats:
    """Best-effort stub: list target resources and seed id_mappings where names match.

    This is a lightweight version that does not attempt full source scan; it only
    ensures the call does not fail and returns empty stats. Real bootstrap is
    handled by the full migration's import pre-checks.
    """
    if resource_type in _SKIP_BOOTSTRAP_TYPES:
        return BootstrapStats(skipped=1)
    # No-op stub for now — full logic would list target and create mappings.
    # Keeping it cheap avoids extra API calls during preview.
    try:
        # Try a single page fetch to validate target is reachable; ignore result.
        endpoint = RESOURCE_REGISTRY.get(resource_type)
        if endpoint and endpoint.endpoint:
            await target_client.get(endpoint.endpoint, params={"page_size": 1})
    except Exception as e:
        logger.debug("bootstrap_stub_skipped", resource_type=resource_type, error=str(e))
    return BootstrapStats()
