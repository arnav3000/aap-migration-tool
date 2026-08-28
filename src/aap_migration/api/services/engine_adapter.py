"""Adapter: Connection -> MigrationConfig/StateConfig for API jobs."""

from __future__ import annotations

from aap_migration.api.models import Connection
from aap_migration.api.services.connection_service import ConnectionService
from aap_migration.config import AAPInstanceConfig, MigrationConfig, StateConfig


def connection_to_aap_config(conn: Connection) -> AAPInstanceConfig:
    """Convert stored Connection to validated AAPInstanceConfig."""
    return ConnectionService.build_instance_config(conn)


def _sqlite_path_from_db_url(db_url: str) -> str:
    """Extract filesystem path from sqlite:///... URL for StateConfig.db_path."""
    # db_url already validated to contain "://"
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://") :]
    # postgres etc — use as-is (MigrationState handles full URL)
    return db_url


def build_migration_config(
    source: AAPInstanceConfig,
    target: AAPInstanceConfig,
    db_url: str,
    *,
    dry_run: bool = False,
    skip_validation: bool = False,
) -> MigrationConfig:
    """Build MigrationConfig for API jobs from two instance configs + API DB URL."""
    db_path = _sqlite_path_from_db_url(db_url)
    # Use a separate path if sqlite to avoid api_state.db lock contention?
    # Reuse same file — MigrationState tables are distinct from api_* tables.
    state_cfg = StateConfig(db_path=db_path)
    # Allow env override for pool settings if postgres
    return MigrationConfig(
        source=source,
        target=target,
        state=state_cfg,
        dry_run=dry_run,
        skip_validation=skip_validation,
    )


def build_migration_config_from_connections(
    source_conn: Connection,
    target_conn: Connection,
    db_url: str | None = None,
    *,
    dry_run: bool = False,
    skip_validation: bool = False,
) -> MigrationConfig:
    """Convenience: build MigrationConfig directly from Connection rows."""
    if db_url is None:
        from aap_migration.api.dependencies import get_db_url

        db_url = get_db_url()
    src_cfg = connection_to_aap_config(source_conn)
    tgt_cfg = connection_to_aap_config(target_conn)
    return build_migration_config(
        src_cfg, tgt_cfg, db_url, dry_run=dry_run, skip_validation=skip_validation
    )
