"""Alembic environment for migration state database schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# Register state models with Base.metadata (API models are managed separately).
import aap_migration.migration.models  # noqa: F401
from aap_migration.migration.models import Base
from alembic import context

# State DB tables managed by Alembic (API tables use create_all in init_database).
STATE_TABLES = frozenset(
    {
        "migration_progress",
        "checkpoints",
        "id_mappings",
        "migration_metadata",
        "performance_metrics",
    }
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("MIGRATION_STATE_DB_PATH") or config.get_main_option("sqlalchemy.url")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table":
        return name in STATE_TABLES
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
