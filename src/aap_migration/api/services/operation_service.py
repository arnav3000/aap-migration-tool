import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from aap_migration.api.models import Connection, Job
from aap_migration.api.services.job_service import JobService


class JobLogHandler(logging.Handler):
    def __init__(self, job_service: JobService, job_id: str) -> None:
        super().__init__()
        self.job_service = job_service
        self.job_id = job_id

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.job_service.append_log(self.job_id, msg)


class OperationService:
    def __init__(
        self,
        job_service: JobService,
        session_factory: sessionmaker[Session],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.job_service = job_service
        self.session_factory = session_factory
        self.loop = loop

    def _get_db_url(self) -> str:
        import os

        return os.environ.get("MIGRATION_STATE_DB_PATH", "sqlite:///aap_bridge.db")

    def _create_job(self, job_type: str, connection_id: str) -> str:
        job_id = str(uuid4())
        db = self.session_factory()
        try:
            job = Job(id=job_id, type=job_type, connection_id=connection_id, status="running")
            db.add(job)
            db.commit()
        finally:
            db.close()
        self.job_service.register_job(job_id)
        return job_id

    def _finish_job(self, job_id: str, status: str, error: str | None = None) -> None:
        db = self.session_factory()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = status
                job.finished_at = datetime.now(UTC)
                job.error = error
                logs = self.job_service.get_logs_since(job_id, 0)
                job.output = logs
                db.commit()
        finally:
            db.close()

    def _attach_log_handler(self, job_id: str) -> JobLogHandler:
        handler = JobLogHandler(self.job_service, job_id)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        return handler

    def _detach_log_handler(self, handler: JobLogHandler) -> None:
        root = logging.getLogger()
        root.removeHandler(handler)

    def _snapshot_connection(self, conn: Connection) -> dict:
        from aap_migration.api.crypto import decrypt_token

        return {
            "id": conn.id,
            "name": conn.name,
            "url": conn.url,
            "token": decrypt_token(conn.token) if conn.token else None,
            "verify_ssl": conn.verify_ssl,
            "type": conn.type,
            "api_prefix": conn.api_prefix,
        }

    def start_cleanup(self, conn: Connection) -> str:
        job_id = self._create_job("cleanup", conn.id)
        snap = self._snapshot_connection(conn)
        db_url = self._get_db_url()

        async def _run() -> None:
            handler = self._attach_log_handler(job_id)
            try:
                from aap_migration.api.services.engine_adapter import connection_to_aap_config
                from aap_migration.cli.commands.cleanup import cancel_all_jobs, delete_resources
                from aap_migration.client.aap_target_client import AAPTargetClient
                from aap_migration.config import AAPInstanceConfig, MigrationConfig, StateConfig

                self.job_service.append_log(
                    job_id, f"Starting cleanup on {snap['name']} ({snap['url']})"
                )

                conn_model = Connection()
                for k, v in snap.items():
                    setattr(conn_model, k, v)

                aap_config = connection_to_aap_config(conn_model)
                target_client = AAPTargetClient(aap_config)

                dummy_source = AAPInstanceConfig(
                    url="https://placeholder.example.com",
                    token="placeholder",
                )
                config = MigrationConfig(
                    source=dummy_source,
                    target=aap_config,
                    state=StateConfig(db_path=db_url),
                )

                self.job_service.append_log(job_id, "Cancelling active jobs...")
                try:
                    result = await cancel_all_jobs(client=target_client, config=config)
                    self.job_service.append_log(job_id, f"Cancelled jobs: {result}")
                except Exception as e:
                    self.job_service.append_log(job_id, f"Warning: cancel_all_jobs: {e}")

                cleanup_types = [
                    "schedules",
                    "workflow_job_templates",
                    "job_templates",
                    "inventory_sources",
                    "hosts",
                    "groups",
                    "inventory",
                    "projects",
                    "credentials",
                    "credential_types",
                    "execution_environments",
                    "teams",
                    "users",
                    "organizations",
                ]

                total_deleted = 0
                total_skipped = 0
                total_errors = 0

                for rt in cleanup_types:
                    self.job_service.append_log(job_id, f"Cleaning up {rt}...")
                    try:
                        deleted, skipped, errors, failed = await delete_resources(
                            client=target_client,
                            resource_type=rt,
                            config=config,
                            skip_default=True,
                        )
                        total_deleted += deleted
                        total_skipped += skipped
                        total_errors += errors
                        self.job_service.append_log(
                            job_id, f"  {rt}: deleted={deleted} skipped={skipped} errors={errors}"
                        )
                    except Exception as e:
                        self.job_service.append_log(job_id, f"  {rt}: error - {e}")
                        total_errors += 1

                self.job_service.append_log(
                    job_id,
                    f"Cleanup complete: deleted={total_deleted} skipped={total_skipped} errors={total_errors}",
                )
                self.job_service.mark_completed(job_id)
                self._finish_job(job_id, "completed")
            except asyncio.CancelledError:
                self.job_service.mark_failed(job_id, "Cancelled")
                self._finish_job(job_id, "cancelled")
            except Exception as e:
                self.job_service.append_log(job_id, f"Cleanup failed: {e}")
                self.job_service.mark_failed(job_id, str(e))
                self._finish_job(job_id, "failed", str(e))
            finally:
                self._detach_log_handler(handler)

        task = self.loop.create_task(_run())
        self.job_service.register_task(job_id, task)
        return job_id

    def start_export(self, conn: Connection) -> str:
        job_id = self._create_job("export", conn.id)
        snap = self._snapshot_connection(conn)

        async def _run() -> None:
            handler = self._attach_log_handler(job_id)
            try:
                import json
                from pathlib import Path

                from aap_migration.api.services.platform_adapter import PlatformAdapter

                self.job_service.append_log(
                    job_id, f"Starting export from {snap['name']} ({snap['url']})"
                )

                conn_model = Connection()
                for k, v in snap.items():
                    setattr(conn_model, k, v)

                adapter = PlatformAdapter(conn_model)
                export_dir = Path(f"./exports/{snap['name'].replace(' ', '_')}")
                export_dir.mkdir(parents=True, exist_ok=True)

                export_types = [
                    "organizations",
                    "teams",
                    "users",
                    "credential_types",
                    "credentials",
                    "projects",
                    "inventories",
                    "inventory_sources",
                    "hosts",
                    "groups",
                    "job_templates",
                    "workflow_job_templates",
                    "schedules",
                    "notification_templates",
                    "labels",
                    "execution_environments",
                ]

                total_exported = 0
                for rt in export_types:
                    self.job_service.append_log(job_id, f"Exporting {rt}...")
                    try:
                        items = await asyncio.to_thread(adapter.fetch_all, rt)
                        if items:
                            rt_dir = export_dir / rt
                            rt_dir.mkdir(parents=True, exist_ok=True)
                            outfile = rt_dir / f"{rt}_001.json"
                            with open(outfile, "w") as f:
                                json.dump(items, f, indent=2, default=str)
                            total_exported += len(items)
                            self.job_service.append_log(job_id, f"  Exported {len(items)} {rt}")
                        else:
                            self.job_service.append_log(job_id, f"  No {rt} found")
                    except Exception as e:
                        self.job_service.append_log(job_id, f"  Error exporting {rt}: {e}")

                self.job_service.append_log(
                    job_id, f"Export complete: {total_exported} resources to {export_dir}"
                )
                self.job_service.mark_completed(job_id)
                self._finish_job(job_id, "completed")
            except asyncio.CancelledError:
                self.job_service.mark_failed(job_id, "Cancelled")
                self._finish_job(job_id, "cancelled")
            except Exception as e:
                self.job_service.append_log(job_id, f"Export failed: {e}")
                self.job_service.mark_failed(job_id, str(e))
                self._finish_job(job_id, "failed", str(e))
            finally:
                self._detach_log_handler(handler)

        task = self.loop.create_task(_run())
        self.job_service.register_task(job_id, task)
        return job_id
