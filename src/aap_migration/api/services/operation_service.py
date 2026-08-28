"""Operation service for export / cleanup jobs (Task 4 clean)."""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import sessionmaker

from aap_migration.api.models import Connection
from aap_migration.api.services.job_service import JobService
from aap_migration.resources import RESOURCE_REGISTRY, get_exportable_types


class OperationService:
    """Handles background export/cleanup via JobService."""

    def __init__(
        self,
        job_service: JobService,
        session_factory: sessionmaker,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.job_service = job_service
        self.session_factory = session_factory
        self.loop = loop

    def _snapshot_connection(self, conn: Connection) -> dict:
        from aap_migration.api.crypto import decrypt_token

        try:
            token = decrypt_token(conn.token)
        except Exception:
            token = conn.token
        return {
            "id": conn.id,
            "name": conn.name,
            "url": conn.url,
            "token": token,
            "verify_ssl": conn.verify_ssl,
            "timeout": conn.timeout,
            "role": conn.role,
        }

    async def start_export(self, conn: Connection, resource_types: list[str] | None = None) -> str:
        snapshot = self._snapshot_connection(conn)

        async def _do_export(append_log) -> dict:
            from aap_migration.client.aap_source_client import AAPSourceClient
            from aap_migration.config import AAPInstanceConfig
            from aap_migration.migration.exporter import create_exporter

            cfg = AAPInstanceConfig(
                url=snapshot["url"],
                token=snapshot["token"],
                verify_ssl=snapshot["verify_ssl"],
                timeout=snapshot["timeout"],
            )
            client = AAPSourceClient(config=cfg)
            # Resolve types
            if resource_types:
                rtypes = [r for r in resource_types if r in RESOURCE_REGISTRY]
                if len(rtypes) != len(resource_types):
                    bad = set(resource_types) - set(rtypes)
                    raise ValueError(f"Unknown resource types: {sorted(bad)}")
            else:
                rtypes = get_exportable_types()

            append_log(f"Starting export from {snapshot['name']} types={rtypes}")
            exported: dict[str, int] = {}
            for rtype in rtypes:
                try:
                    # Use exporter if available, else fallback to paginated fetch
                    try:
                        # need a lightweight state object; use None for performance config?
                        from aap_migration.config import PerformanceConfig
                        from aap_migration.migration.state import MigrationState

                        # Use a dummy state with in-memory db for counting only
                        state = MigrationState(db_path=":memory:")
                        exp = create_exporter(rtype, client, state, PerformanceConfig())
                        count = await exp.get_count(RESOURCE_REGISTRY[rtype].endpoint)
                        append_log(f"{rtype}: {count} resources")
                        exported[rtype] = count
                    except Exception:
                        # Fallback to generic count via client
                        resp = await client.get(f"{rtype}/", params={"page_size": 1})
                        count = resp.get("count", 0)
                        append_log(f"{rtype}: {count} resources (fallback)")
                        exported[rtype] = count
                except Exception as e:
                    append_log(f"{rtype}: error {e}")
                    exported[rtype] = 0
            append_log(f"Export complete total={sum(exported.values())}")
            return {
                "connection_id": snapshot["id"],
                "exported": exported,
                "total": sum(exported.values()),
            }

        job = await self.job_service.start_job(
            "export", _do_export, name=f"export:{snapshot['name']}"
        )
        return job.job_id

    async def start_cleanup(self, conn: Connection, resource_types: list[str]) -> str:
        snapshot = self._snapshot_connection(conn)
        # Validate
        for rt in resource_types:
            if rt not in RESOURCE_REGISTRY:
                raise ValueError(f"Unknown resource type: {rt}")

        async def _do_cleanup(append_log) -> dict:
            from aap_migration.client.aap_target_client import AAPTargetClient
            from aap_migration.config import AAPInstanceConfig

            cfg = AAPInstanceConfig(
                url=snapshot["url"],
                token=snapshot["token"],
                verify_ssl=snapshot["verify_ssl"],
                timeout=snapshot["timeout"],
            )
            client = AAPTargetClient(config=cfg)
            # Order by cleanup_order descending
            ordered = sorted(
                resource_types, key=lambda r: RESOURCE_REGISTRY[r].cleanup_order, reverse=True
            )
            append_log(f"Starting cleanup on {snapshot['name']} types={ordered}")
            deleted: dict[str, int] = {}
            for rtype in ordered:
                try:
                    # Attempt bulk delete via paginated list + delete each
                    endpoint = RESOURCE_REGISTRY[rtype].endpoint
                    resp = await client.get(endpoint, params={"page_size": 200})
                    items = resp.get("results", [])
                    count = 0
                    for item in items:
                        try:
                            await client.delete(f"{endpoint}{item['id']}/")
                            count += 1
                        except Exception as e:
                            append_log(f"delete {rtype}/{item.get('id')} failed: {e}")
                    append_log(f"{rtype}: deleted {count}/{len(items)}")
                    deleted[rtype] = count
                except Exception as e:
                    append_log(f"{rtype}: cleanup error {e}")
                    deleted[rtype] = 0
            append_log(f"Cleanup complete total_deleted={sum(deleted.values())}")
            return {"connection_id": snapshot["id"], "deleted": deleted}

        job = await self.job_service.start_job(
            "cleanup", _do_cleanup, name=f"cleanup:{snapshot['name']}"
        )
        return job.job_id
