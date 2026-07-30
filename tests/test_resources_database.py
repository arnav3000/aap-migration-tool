from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from aap_migration.api.models import JobRecord
from aap_migration.client.exceptions import ConfigurationError, StateError
from aap_migration.migration import database as migration_database
from aap_migration.migration.database import (
    create_database_backup,
    create_database_engine,
    get_database_size,
    get_engine,
    get_session,
    get_session_factory,
    init_database,
    reset_database,
    validate_database_connection,
)
from aap_migration.resources import (
    EXPORTABLE_TYPES,
    FULLY_SUPPORTED_TYPES,
    apply_host_membership_resource_cascade,
    cascade_skip_phases_for_hosts,
    get_all_types,
    get_batch_size,
    get_cleanup_order,
    get_description,
    get_discovered_types,
    get_endpoint,
    get_exportable_types,
    get_fully_supported_types,
    get_importable_types,
    get_info,
    get_migration_order,
    get_transformable_types,
    has_discovered_endpoints,
    is_host_inventory_membership_excluded,
    is_valid_type,
    normalize_resource_type,
)


def test_resource_registry_fallback_helpers() -> None:
    assert "organizations" in get_all_types()
    assert get_endpoint("organizations") == "organizations/"
    assert get_info("hosts").use_bulk_api is True
    assert get_batch_size("hosts") == 200
    assert get_description("users") == "Users"
    assert is_valid_type("users") is True
    assert is_valid_type("not-real") is False
    assert normalize_resource_type("groups") == "inventory_groups"
    assert normalize_resource_type("organizations") == "organizations"
    assert "organizations" in get_migration_order()
    assert "jobs" in get_cleanup_order()
    assert "credentials" in get_transformable_types()
    assert "organizations" in get_fully_supported_types()
    assert EXPORTABLE_TYPES == get_exportable_types()
    assert FULLY_SUPPORTED_TYPES == get_fully_supported_types()


def test_host_membership_exclusion_cascade() -> None:
    assert cascade_skip_phases_for_hosts(["hosts"]) == [
        "hosts",
        "host_inventory_memberships",
    ]
    assert cascade_skip_phases_for_hosts(["credentials"]) == ["credentials"]

    order = ["hosts", "host_inventory_memberships", "job_templates"]
    assert apply_host_membership_resource_cascade(
        ["host_inventory_memberships", "job_templates"]
    ) == ["job_templates"]
    assert apply_host_membership_resource_cascade(order) == order
    assert apply_host_membership_resource_cascade(
        order,
        exclusions={"hosts": [1, 2]},
        preview_resources={
            "hosts": [{"source_id": 1}, {"source_id": 2}],
        },
    ) == ["hosts", "job_templates"]
    assert (
        apply_host_membership_resource_cascade(
            order,
            exclusions={"hosts": [1]},
            preview_resources={
                "hosts": [{"source_id": 1}, {"source_id": 2}],
            },
        )
        == order
    )

    assert is_host_inventory_membership_excluded({"host_id": 9, "inventory_id": 3}, {"hosts": [9]})
    assert not is_host_inventory_membership_excluded(
        {"host_id": 9, "inventory_id": 3}, {"hosts": [8]}
    )
    assert not is_host_inventory_membership_excluded({"host_id": 9, "inventory_id": 3}, None)

    from aap_migration.resources import (
        excluded_preview_count,
        is_resource_type_fully_excluded,
    )

    preview = {"hosts": [{"source_id": 1}, {"source_id": 2}]}
    assert is_resource_type_fully_excluded("hosts", {"hosts": [1, 2]}, preview)
    assert not is_resource_type_fully_excluded("hosts", {"hosts": [1]}, preview)
    assert excluded_preview_count("hosts", {"hosts": [1]}, preview) == 1


def test_resource_helpers_use_discovered_endpoints(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "source_endpoints.json").write_text(
        json.dumps(
            {
                "endpoints": {
                    "inventory": {"url": "/api/v2/inventories/"},
                    "groups": {"url": "/api/v2/groups/"},
                    "jobs": {"url": "/api/v2/jobs/"},
                }
            }
        )
    )
    (schemas / "target_endpoints.json").write_text(
        json.dumps({"endpoints": {"custom_resource": {"url": "/api/v2/custom/"}}})
    )
    monkeypatch.chdir(tmp_path)

    assert has_discovered_endpoints() is True
    assert set(get_discovered_types()) == {"inventory", "groups", "jobs"}
    assert get_exportable_types(use_discovered=True) == ["inventories", "inventory_groups"]
    assert get_importable_types(use_discovered=True) == ["custom_resource"]
    assert get_endpoint("inventory") == "/api/v2/inventories/"


def test_database_engine_and_session_helpers(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'state.db'}"
    engine = create_database_engine(db_url)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    engine.dispose()

    init_database(db_url)
    assert get_engine() is migration_database._engine
    assert get_session_factory() is migration_database._SessionFactory

    with get_session() as session:
        session.add(JobRecord(id="job-1", seq_id=1, name="Job", type="demo", status="completed"))

    with get_session() as session:
        assert session.get(JobRecord, "job-1") is not None


def test_get_session_wraps_errors_in_state_error(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'state.db'}"
    init_database(db_url)

    with pytest.raises(StateError, match="Database operation failed"):
        with get_session() as session:
            session.add(
                JobRecord(id="job-1", seq_id=1, name="Job", type="demo", status="completed")
            )
            raise RuntimeError("boom")


def test_database_validation_size_backup_and_reset(tmp_path) -> None:
    db_file = tmp_path / "state.db"
    db_url = f"sqlite:///{db_file}"
    init_database(db_url)

    assert validate_database_connection(db_url) is True
    assert get_database_size(db_url) >= 0

    backup = tmp_path / "backups" / "state-backup.db"
    create_database_backup(db_url, str(backup))
    assert backup.exists() is True

    reset_database(db_url)
    assert validate_database_connection(db_url) is True


def test_database_error_branches(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="Database URL cannot be empty"):
        create_database_engine("")

    with pytest.raises(ConfigurationError, match="Database engine not initialized"):
        migration_database._engine = None
        get_engine()

    with pytest.raises(ConfigurationError, match="Session factory not initialized"):
        migration_database._SessionFactory = None
        get_session_factory()

    assert validate_database_connection("not-a-real-dialect:///bad") is False

    with pytest.raises(ValueError, match="only supported for SQLite"):
        get_database_size("postgresql://user:pass@db/example")

    with pytest.raises(ValueError, match="only supported for SQLite"):
        create_database_backup("postgresql://user:pass@db/example", str(tmp_path / "backup.db"))

    with pytest.raises(ConfigurationError, match="Database file not found"):
        create_database_backup(f"sqlite:///{tmp_path / 'missing.db'}", str(tmp_path / "backup.db"))
