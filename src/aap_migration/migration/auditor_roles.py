"""Post-import Gateway auditor role assignment for AAP 2.6+.

AAP 2.6 moved "System Auditor" from a Controller boolean field
(is_system_auditor) to a Gateway role_user_assignment. The migration
pipeline's user POST sets the Controller field, but AAP 2.6 ignores it
without a corresponding Gateway assignment. This module creates those
assignments after user import completes.

Gate assumption: the transformer emits is_system_auditor as a real Python
bool (True/False). The gate uses ``is True`` deliberately — non-bool truthy
values (strings, ints) are skipped by design to avoid false positives.

Used by:
  - export_import.py (automatic post-phase pass after user import)
  - tools/remediate_auditor_roles.py (standalone for already-migrated envs)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)

PLATFORM_AUDITOR_ROLE_NAME = "Platform Auditor"
GATEWAY_ASSIGNMENTS_ENDPOINT = "role_user_assignments/"
GATEWAY_ROLE_DEFS_ENDPOINT = "role_definitions/"
CONTROLLER_USERS_ENDPOINT = "users/"

SYNC_RETRY_MAX = 5
SYNC_RETRY_INTERVAL_S = 0.5


@dataclass
class AuditorAssignmentResult:
    """Result of a single auditor Gateway assignment attempt."""

    username: str
    source_id: int
    target_id: int
    success: bool
    gateway_assignment_id: int | None = None
    controller_synced: bool = False
    sync_latency_ms: float | None = None
    error: str | None = None


@dataclass
class AuditorRolesSummary:
    """Summary of all auditor role assignments for reporting."""

    auditor_count: int = 0
    assigned_count: int = 0
    verified_count: int = 0
    failed: list[AuditorAssignmentResult] = field(default_factory=list)
    role_definition_id: int | None = None
    sync_latency_ms_max: float = 0.0


def _gateway_url(controller_base_url: str) -> str:
    return controller_base_url.replace("/api/controller/v2", "/api/gateway/v1")


def create_preflight_failure_summary(
    auditor_sources: list[dict[str, Any]],
    error: str,
) -> AuditorRolesSummary:
    """Mark all auditors as failed when Gateway preflight is denied."""
    summary = AuditorRolesSummary(auditor_count=len(auditor_sources))
    for src in auditor_sources:
        result = AuditorAssignmentResult(
            username=src.get("username", "unknown"),
            source_id=src.get("_source_id", src.get("id", 0)),
            target_id=0,
            success=False,
            error=f"Gateway preflight failed: {error}",
        )
        summary.failed.append(result)
    return summary


async def preflight_gateway_access(
    client: Any,
) -> int:
    """Resolve Platform Auditor role_definition ID and verify Gateway access.

    Returns the role_definition ID on success.
    Raises RuntimeError on 401/403 (token lacks Gateway access) or if
    the Platform Auditor role is not found.
    """
    gateway_base = _gateway_url(client.base_url)
    endpoint = f"{gateway_base}/{GATEWAY_ROLE_DEFS_ENDPOINT}"

    response = await client.client.get(
        endpoint,
        params={"name": PLATFORM_AUDITOR_ROLE_NAME},
        headers={"Authorization": f"Bearer {client.token}"},
    )

    if response.status_code in (401, 403):
        raise RuntimeError(
            f"Gateway API returned {response.status_code} — the migration "
            f"token lacks Gateway access. A Gateway-capable token is required "
            f"to assign Platform Auditor roles. Verify TARGET__TOKEN has "
            f"Gateway admin privileges."
        )

    response.raise_for_status()
    data = response.json()
    results = data.get("results", [])

    if not results:
        raise RuntimeError(
            f"Platform Auditor role_definition not found on target. "
            f"GET {endpoint}?name=Platform+Auditor returned 0 results."
        )

    role_def_id = results[0]["id"]
    logger.info(
        "gateway_preflight_ok",
        role_definition_id=role_def_id,
        role_name=PLATFORM_AUDITOR_ROLE_NAME,
    )
    return role_def_id


async def _create_gateway_assignment(
    client: Any,
    target_user_id: int,
    role_definition_id: int,
) -> dict[str, Any]:
    """POST a Gateway role_user_assignment. Returns the response dict.

    AAP 2.6 returns 201 for both new and existing assignments (get-or-create).
    """
    gateway_base = _gateway_url(client.base_url)
    endpoint = f"{gateway_base}/{GATEWAY_ASSIGNMENTS_ENDPOINT}"

    response = await client.client.post(
        endpoint,
        json={"user": target_user_id, "role_definition": role_definition_id},
        headers={"Authorization": f"Bearer {client.token}"},
    )
    response.raise_for_status()
    return response.json()


async def _verify_controller_sync(
    client: Any,
    target_user_id: int,
) -> tuple[bool, float]:
    """Poll Controller until is_system_auditor=True or retries exhausted.

    Returns (synced: bool, latency_ms: float).
    """
    start = time.monotonic()
    for attempt in range(SYNC_RETRY_MAX):
        user_data = await client.get(f"{CONTROLLER_USERS_ENDPOINT}{target_user_id}/")
        if user_data.get("is_system_auditor") is True:
            latency_ms = (time.monotonic() - start) * 1000
            return True, latency_ms
        if attempt < SYNC_RETRY_MAX - 1:
            await asyncio.sleep(SYNC_RETRY_INTERVAL_S)
    latency_ms = (time.monotonic() - start) * 1000
    return False, latency_ms


async def assign_auditor_roles(
    client: Any,
    auditor_users: list[dict[str, Any]],
    role_definition_id: int,
) -> AuditorRolesSummary:
    """Assign Gateway Platform Auditor roles and verify Controller sync.

    Args:
        client: AAPTargetClient with Gateway-capable token
        auditor_users: List of dicts with keys: username, source_id, target_id
        role_definition_id: Resolved Platform Auditor role_definition ID

    Returns:
        AuditorRolesSummary with per-user results
    """
    summary = AuditorRolesSummary(
        auditor_count=len(auditor_users),
        role_definition_id=role_definition_id,
    )

    for user in auditor_users:
        username = user["username"]
        source_id = user["source_id"]
        target_id = user["target_id"]

        result = AuditorAssignmentResult(
            username=username,
            source_id=source_id,
            target_id=target_id,
            success=False,
        )

        try:
            gw_response = await _create_gateway_assignment(
                client, target_id, role_definition_id
            )
            result.gateway_assignment_id = gw_response.get("id")
            summary.assigned_count += 1

            synced, latency_ms = await _verify_controller_sync(client, target_id)
            result.controller_synced = synced
            result.sync_latency_ms = latency_ms
            if latency_ms > summary.sync_latency_ms_max:
                summary.sync_latency_ms_max = latency_ms

            if synced:
                result.success = True
                summary.verified_count += 1
                logger.info(
                    "auditor_assignment_verified",
                    username=username,
                    target_id=target_id,
                    gateway_assignment_id=result.gateway_assignment_id,
                    sync_latency_ms=round(latency_ms, 1),
                )
            else:
                result.error = (
                    f"Gateway assignment created (id={result.gateway_assignment_id}) "
                    f"but Controller is_system_auditor did not sync to True "
                    f"after {SYNC_RETRY_MAX} retries ({latency_ms:.0f}ms)"
                )
                summary.failed.append(result)
                logger.warning(
                    "auditor_sync_timeout",
                    username=username,
                    target_id=target_id,
                    gateway_assignment_id=result.gateway_assignment_id,
                    retries=SYNC_RETRY_MAX,
                    latency_ms=round(latency_ms, 1),
                )

        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            summary.failed.append(result)
            logger.error(
                "auditor_assignment_failed",
                username=username,
                target_id=target_id,
                error=result.error,
            )

    return summary
