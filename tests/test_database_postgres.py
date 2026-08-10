"""PostgreSQL integration tests for migration state database (Alembic + init_database)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text

from aap_migration.config import StateConfig
from aap_migration.migration import database as migration_database
from aap_migration.migration.checkpoint import CheckpointManager
from aap_migration.migration.database import (
    STATE_TABLES,
    create_database_engine,
    init_database,
    reset_database,
    validate_database_connection,
)
from aap_migration.migration.state import MigrationState

DEFAULT_COMPOSE_URL = "postgresql://aap_user:changeme@localhost:5432/aap_migration"
INITIAL_REVISION = "b8e1e6f93880"


def _resolve_postgres_url() -> str | None:
    for key in ("TEST_DATABASE_URL", "MIGRATION_STATE_DB_PATH"):
        url = os.environ.get(key)
        if url and url.startswith("postgresql"):
            return url
    return DEFAULT_COMPOSE_URL


def _reset_globals() -> None:
    if migration_database._engine is not None:
        migration_database._engine.dispose()
    migration_database._engine = None
    migration_database._SessionFactory = None


@pytest.fixture(scope="module")
def postgres_db_url() -> str:
    url = _resolve_postgres_url()
    if url is None or not validate_database_connection(url):
        pytest.skip("PostgreSQL not available (set TEST_DATABASE_URL or start compose db)")
    return url


@pytest.fixture
def clean_postgres_db(postgres_db_url: str) -> str:
    _reset_globals()
    reset_database(postgres_db_url)
    yield postgres_db_url
    _reset_globals()


def _build_state(db_url: str, migration_id: str, source_key: str = "") -> MigrationState:
    return MigrationState(
        StateConfig(db_path=db_url),
        migration_id=migration_id,
        source_key=source_key,
    )


@pytest.mark.postgres
def test_postgres_init_database_applies_alembic_migrations(clean_postgres_db: str) -> None:
    engine = init_database(clean_postgres_db)

    inspector = inspect(engine)
    for table in STATE_TABLES:
        assert inspector.has_table(table), f"Missing state table: {table}"
    assert inspector.has_table("alembic_version")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == INITIAL_REVISION


@pytest.mark.postgres
def test_postgres_source_key_scoped_mappings(clean_postgres_db: str) -> None:
    init_database(clean_postgres_db)

    state_a = _build_state(clean_postgres_db, migration_id="mig-a", source_key="conn-a")
    state_b = _build_state(clean_postgres_db, migration_id="mig-b", source_key="conn-b")

    state_a.mark_completed("organizations", 1, 101, source_name="Org A")
    state_b.mark_completed("organizations", 1, 201, source_name="Org B")

    assert state_a.get_mapped_id("organizations", 1) == 101
    assert state_b.get_mapped_id("organizations", 1) == 201


@pytest.mark.postgres
def test_postgres_checkpoint_round_trip(clean_postgres_db: str) -> None:
    init_database(clean_postgres_db)

    state = _build_state(clean_postgres_db, migration_id="checkpoint-mig")
    state.mark_in_progress("inventories", 1, "Inventory 1")
    state.mark_completed("inventories", 1, 101, source_name="Inventory 1")

    manager = CheckpointManager(state)
    checkpoint_id = manager.create_checkpoint(
        phase="inventories",
        checkpoint_data={"last_processed_id": 1},
        description="PostgreSQL checkpoint test",
    )

    restored = manager.restore_checkpoint(checkpoint_id)
    assert restored["phase"] == "inventories"
    assert restored["checkpoint_data"]["last_processed_id"] == 1
    assert manager.get_latest_checkpoint() is not None


@pytest.mark.postgres
def test_postgres_legacy_schema_stamp_path(clean_postgres_db: str) -> None:
    """Existing create_all DBs without alembic_version are stamped at head."""
    init_database(clean_postgres_db)

    engine = create_database_engine(clean_postgres_db)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    engine.dispose()

    _reset_globals()
    init_database(clean_postgres_db)

    engine = create_database_engine(clean_postgres_db)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("alembic_version")
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == INITIAL_REVISION
    finally:
        engine.dispose()
