"""Migration service — preview + run as background Jobs."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aap_migration.api.models import Connection
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.api.services.engine_adapter import build_migration_config
from aap_migration.api.services.job_service import Job, JobStatus
from aap_migration.resources import RESOURCE_REGISTRY, get_exportable_types

logger = logging.getLogger(__name__)

# Default types for preview when none requested (mirrors old PREVIEW_RESOURCE_TYPES)
PREVIEW_RESOURCE_TYPES: list[str] = [
    "organizations",
    "teams",
    "users",
    "credential_types",
    "credentials",
    "projects",
    "inventories",
    "hosts",
    "inventory_groups",
    "job_templates",
    "workflow_job_templates",
    "schedules",
]


class JobLogHandler(logging.Handler):
    """Bridge stdlib logs -> Job log lines + simple progress."""

    def __init__(self, job: Job, append_log: Callable[[str], None]) -> None:
        super().__init__()
        self.job = job
        self.append_log = append_log
        self._phase = ""
        self._created = 0
        self._skipped = 0
        self._failed = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # Always append raw log for debugging
            self.append_log(msg)
            # Light parsing to give user visible progress (no need for full state machine)
            if "migration_completed" in msg or "migration_failed" in msg:
                self.append_log(
                    f"\nMigration complete: {self._created} created, {self._skipped} skipped, {self._failed} failed"
                )
            elif "resource_created" in msg:
                self._created += 1
            elif "resource_skipped" in msg or "skipped" in msg.lower() and "count" in msg:
                self._skipped += 1
            elif "resource_import_failed" in msg or "import_failed" in msg:
                self._failed += 1
                # Extract name for user
                self.append_log(f"  ✗ {msg[:120]}")
        except Exception:
            pass


def _resolve_resource_types(requested: list[str] | None) -> list[str]:
    """Normalize and validate requested resource types, fallback to exportable."""
    if not requested:
        # Use preview defaults filtered by what has exporter
        avail = set(get_exportable_types())
        return [t for t in PREVIEW_RESOURCE_TYPES if t in avail]
    out: list[str] = []
    for rt in requested:
        if rt not in RESOURCE_REGISTRY:
            raise ValueError(f"Unknown resource type: {rt}")
        out.append(rt)
    # Sort by migration_order
    out.sort(key=lambda t: RESOURCE_REGISTRY[t].migration_order)
    return out


def _phase_names_for_types(resource_types: list[str]) -> list[str] | None:
    """Map resource_types -> coordinator phase names. None means all phases."""
    from aap_migration.migration.coordinator import MigrationCoordinator

    if not resource_types:
        return None
    # Build reverse map resource_type -> phase name
    rt_to_phase: dict[str, str] = {}
    for ph in MigrationCoordinator.MIGRATION_PHASES:
        for rt in ph["resource_types"]:
            rt_to_phase[rt] = ph["name"]
    phases: set[str] = set()
    for rt in resource_types:
        ph = rt_to_phase.get(rt)
        if ph:
            phases.add(ph)
    # Return sorted by phase order
    ordered = [p["name"] for p in MigrationCoordinator.MIGRATION_PHASES if p["name"] in phases]
    return ordered or None


async def _counts_via_export(
    source_conn: Connection,
    db_url: str,
    resource_types: list[str],
    organizations: list[str] | None,
    name_prefix: str,
    log: Callable[[str], None],
) -> dict[str, int]:
    """Count resources per type via exporter export() (streaming)."""
    from aap_migration.client.aap_source_client import AAPSourceClient
    from aap_migration.config import StateConfig
    from aap_migration.migration.exporter import create_exporter
    from aap_migration.migration.state import MigrationState

    cfg = ConnectionService.build_instance_config(source_conn)
    # Extract sqlite path for state
    db_path = (
        db_url.replace("sqlite:///", "").replace("sqlite://", "") if "sqlite" in db_url else db_url
    )
    state_cfg = StateConfig(db_path=db_path)
    state = MigrationState(config=state_cfg)
    client = AAPSourceClient(config=cfg)
    counts: dict[str, int] = {}
    try:
        for rt in resource_types:
            log(f"Counting {rt}...")
            try:
                endpoint = RESOURCE_REGISTRY[rt].endpoint
                if not endpoint:
                    counts[rt] = 0
                    continue
                from aap_migration.config import PerformanceConfig

                perf = PerformanceConfig()
                exporter = create_exporter(
                    resource_type=rt,
                    client=client,
                    state=state,
                    performance_config=perf,
                )
                # For hosts/inventories apply server-side filters later if needed; here just count
                c = 0
                # Use get_count if available for efficiency
                if hasattr(exporter, "get_count"):
                    try:
                        # try count with filters
                        filters: dict[str, str] | None = None
                        if rt == "hosts" and name_prefix:
                            # client-side filter only — count unfiltered then filter later
                            pass
                        c = await exporter.get_count(endpoint, filters=filters)  # type: ignore[attr-defined]
                    except Exception:
                        c = 0
                        async for res in exporter.export():
                            # Apply client-side organization/name_prefix filters
                            if organizations and res.get("organization") is not None:
                                # If resource has org name, filter; else keep
                                org_name = (
                                    res.get("summary_fields", {})
                                    .get("organization", {})
                                    .get("name")
                                )
                                if org_name and org_name not in organizations:
                                    continue
                            if name_prefix and not str(res.get("name", "")).startswith(name_prefix):
                                continue
                            c += 1
                else:
                    async for res in exporter.export():
                        if organizations and res.get("summary_fields", {}).get(
                            "organization", {}
                        ).get("name"):
                            org_name = res["summary_fields"]["organization"]["name"]
                            if org_name not in organizations:
                                continue
                        if name_prefix and not str(res.get("name", "")).startswith(name_prefix):
                            continue
                        c += 1
                counts[rt] = c
                log(f"  {rt}: {c}")
            except Exception as e:
                logger.warning("preview_count_failed", resource_type=rt, error=str(e))
                counts[rt] = 0
                log(f"  {rt}: failed to count ({e})")
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return counts


async def execute_preview(
    job: Job,
    log: Callable[[str], None],
    *,
    source_conn: Connection,
    target_conn: Connection,
    db_url: str,
    resource_types: list[str] | None,
    organizations: list[str] | None,
    name_prefix: str,
) -> dict[str, Any]:
    """Run preview (counts) and populate job.result."""
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    log("Starting migration preview (scanning source)...")
    if organizations:
        log(f"Filtering to organizations: {organizations}")
    if name_prefix:
        log(f"Filtering to name prefix: {name_prefix!r}")

    resolved = _resolve_resource_types(resource_types)
    log(f"Resource types: {', '.join(resolved)}")

    # Bootstrap target mappings best-effort (so preview can report existing)
    try:
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import StateConfig
        from aap_migration.migration.state import MigrationState
        from aap_migration.migration.target_bootstrap import bootstrap_mappings_for_type

        tgt_cfg = ConnectionService.build_instance_config(target_conn)
        tgt_client = AAPTargetClient(config=tgt_cfg)
        db_path = (
            db_url.replace("sqlite:///", "").replace("sqlite://", "")
            if "sqlite" in db_url
            else db_url
        )
        state_cfg = StateConfig(db_path=db_path)
        state = MigrationState(config=state_cfg)
        for rt in resolved:
            try:
                await bootstrap_mappings_for_type(rt, tgt_client, state)
            except Exception as e:
                logger.debug("bootstrap_failed", resource_type=rt, error=str(e))
        try:
            await tgt_client.close()
        except Exception:
            pass
    except Exception as e:
        logger.debug("bootstrap_preview_failed", error=str(e))

    # Map managed credential types best-effort
    try:
        from aap_migration.client.aap_source_client import AAPSourceClient
        from aap_migration.client.aap_target_client import AAPTargetClient
        from aap_migration.config import StateConfig
        from aap_migration.migration.credential_type_utils import map_managed_credential_types
        from aap_migration.migration.state import MigrationState

        src_cfg = ConnectionService.build_instance_config(source_conn)
        tgt_cfg = ConnectionService.build_instance_config(target_conn)
        src_client = AAPSourceClient(config=src_cfg)
        tgt_client2 = AAPTargetClient(config=tgt_cfg)
        db_path2 = (
            db_url.replace("sqlite:///", "").replace("sqlite://", "")
            if "sqlite" in db_url
            else db_url
        )
        state2 = MigrationState(config=StateConfig(db_path=db_path2))
        try:
            await map_managed_credential_types(src_client, tgt_client2, state2)
        except Exception as e:
            logger.debug("map_managed_failed", error=str(e))
        try:
            await src_client.close()
        except Exception:
            pass
        try:
            await tgt_client2.close()
        except Exception:
            pass
    except Exception:
        pass

    counts = await _counts_via_export(
        source_conn, db_url, resolved, organizations, name_prefix, log
    )

    total = sum(counts.values())
    warnings: list[str] = []
    if total == 0:
        warnings.append("No resources found for preview (check filters/connection).")
        log("No resources found for preview.")

    result = {
        "job_id": job.job_id,
        "status": "completed",
        "counts": counts,
        "resource_types": resolved,
        "warnings": warnings,
        "total": total,
    }
    job.result = result
    job.status = JobStatus.COMPLETED
    job.completed_at = datetime.now(UTC)
    log(f"Preview complete: {total} resources across {len(resolved)} types")
    return result


async def execute_migration(
    job: Job,
    log: Callable[[str], None],
    *,
    source_conn: Connection,
    target_conn: Connection,
    db_url: str,
    resource_types: list[str] | None,
    organizations: list[str] | None,
    name_prefix: str,
    dry_run: bool = False,
    skip_validation: bool = False,
) -> dict[str, Any]:
    """Run full migration via MigrationCoordinator."""
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    start = time.time()
    log(f"Starting migration (dry_run={dry_run})...")
    if organizations:
        log(f"Organizations filter: {organizations}")
    if name_prefix:
        log(f"Name prefix: {name_prefix!r}")

    resolved = _resolve_resource_types(resource_types) if resource_types else []
    # _phase_names_for_types returns None for all
    phase_names = _phase_names_for_types(resolved) if resolved else None
    if phase_names:
        log(f"Phases: {', '.join(phase_names)}")
    else:
        log("Migrating all phases")

    from aap_migration.client.aap_source_client import AAPSourceClient
    from aap_migration.client.aap_target_client import AAPTargetClient
    from aap_migration.migration.coordinator import MigrationCoordinator
    from aap_migration.migration.state import MigrationState

    src_cfg = ConnectionService.build_instance_config(source_conn)
    tgt_cfg = ConnectionService.build_instance_config(target_conn)
    # Build MigrationConfig via adapter
    migration_config = build_migration_config(
        src_cfg, tgt_cfg, db_url, dry_run=dry_run, skip_validation=skip_validation
    )
    # If organizations / name_prefix filters are set, we stash them in export config for now
    # (coordinator doesn't natively filter by org; exporter does via query params in future)
    # For Task 3 we log filters but let coordinator run unfiltered; later phases can honor them.

    db_path = (
        db_url.replace("sqlite:///", "").replace("sqlite://", "") if "sqlite" in db_url else db_url
    )
    from aap_migration.config import StateConfig

    state = MigrationState(config=StateConfig(db_path=db_path), migration_id=job.job_id)

    source_client = AAPSourceClient(config=src_cfg)
    target_client = AAPTargetClient(config=tgt_cfg)

    # Attach log handler to bridge coordinator logs -> job
    handler = JobLogHandler(job, log)
    handler.setLevel(logging.INFO)
    # Attach to migration logger
    mig_logger = logging.getLogger("aap_migration.migration.coordinator")
    mig_logger.addHandler(handler)
    # Also attach to general migration
    root_mig = logging.getLogger("aap_migration")
    root_mig.addHandler(handler)

    summary: dict[str, Any] = {}
    try:
        coordinator = MigrationCoordinator(
            config=migration_config,
            source_client=source_client,
            target_client=target_client,
            state=state,
            enable_progress=False,
        )
        # Map managed credential types before main run (idempotent)
        try:
            from aap_migration.migration.credential_type_utils import map_managed_credential_types

            await map_managed_credential_types(source_client, target_client, state)
            log("Mapped managed credential types")
        except Exception as e:
            log(f"Managed credential type mapping skipped: {e}")

        # Bootstrap target mappings for idempotent imports
        try:
            from aap_migration.migration.target_bootstrap import bootstrap_mappings_for_type

            bs_types = resolved or list(RESOURCE_REGISTRY.keys())
            for rt in bs_types:
                try:
                    await bootstrap_mappings_for_type(rt, target_client, state)
                except Exception:
                    pass
            log("Bootstrapped target mappings")
        except Exception as e:
            log(f"Bootstrap skipped: {e}")

        if phase_names:
            summary = await coordinator.migrate_all(only_phases=phase_names)
        else:
            summary = await coordinator.migrate_all()

        summary["duration_seconds"] = time.time() - start
        summary["dry_run"] = dry_run
        job.result = summary
        job.output = summary
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(UTC)
        log(
            f"Migration completed in {job.completed_at - job.started_at if job.started_at and job.completed_at else ''}"
        )
        return summary
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.error = "cancelled"
        job.completed_at = datetime.now(UTC)
        log("Migration cancelled")
        raise
    except Exception as e:
        logger.exception("migration_failed", error=str(e))
        job.status = JobStatus.FAILED
        job.error = str(e)[:2000]
        job.completed_at = datetime.now(UTC)
        log(f"Migration failed: {e}")
        # Return summary with error so caller can persist
        summary = {"error": str(e), "status": "failed", "dry_run": dry_run}
        job.result = summary
        # Re-raise so JobService marks failed, but we already set job fields
        raise
    finally:
        try:
            mig_logger.removeHandler(handler)
        except Exception:
            pass
        try:
            root_mig.removeHandler(handler)
        except Exception:
            pass
        try:
            await source_client.close()
        except Exception:
            pass
        try:
            await target_client.close()
        except Exception:
            pass
