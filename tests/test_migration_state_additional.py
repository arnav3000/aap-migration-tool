import json

import pytest

from aap_migration.config import StateConfig
from aap_migration.migration.database import get_session
from aap_migration.migration.models import IDMapping, MigrationProgress
from aap_migration.migration.state import MigrationState, StateError


def build_state(db_url: str, migration_id: str = "test-migration") -> MigrationState:
    return MigrationState(StateConfig(db_path=db_url), migration_id=migration_id)


def get_progress(
    state: MigrationState, resource_type: str, source_id: int
) -> MigrationProgress | None:
    with get_session(state.database_url) as session:
        return (
            session.query(MigrationProgress)
            .filter_by(resource_type=resource_type, source_id=source_id)
            .first()
        )


def get_mapping(state: MigrationState, resource_type: str, source_id: int) -> IDMapping | None:
    with get_session(state.database_url) as session:
        return (
            session.query(IDMapping)
            .filter_by(resource_type=resource_type, source_id=source_id)
            .first()
        )


def test_state_tracks_lifecycle_and_partial_imports(sqlite_db_url):
    state = build_state(sqlite_db_url)

    assert state.is_migrated("projects", 1) is False
    assert state.has_mapping("projects", 1) is False

    state.mark_in_progress("projects", 1, "Project One", phase="transform")
    assert state.is_migrated("projects", 1) is True
    assert state.get_status("projects", 1) == "in_progress"

    state.mark_failed("projects", 1, "boom")
    failed_progress = get_progress(state, "projects", 1)
    assert failed_progress.status == "failed"
    assert failed_progress.retry_count == 1

    assert state.reset_failed("projects") == 1
    reset_progress = get_progress(state, "projects", 1)
    assert reset_progress.status == "pending"
    assert reset_progress.started_at is None
    assert reset_progress.completed_at is None

    state.mark_completed("projects", 1, 101, target_name="Project One Target")
    assert state.get_status("projects", 1) == "completed"
    assert state.get_mapped_id("projects", 1) == 101
    assert state.get_imported_source_ids("projects") == {1}
    assert state.get_import_stats("projects") == {
        "total_exported": 1,
        "total_imported": 1,
        "pending": 0,
        "percent_complete": 100.0,
    }

    state.update_mapping_target_id("projects", 1, 202, target_name="Project One Updated")
    assert state.get_id_mapping("projects", 1) == {
        "source_id": 1,
        "target_id": 202,
        "source_name": "Project One",
        "target_name": "Project One Updated",
        "resource_type": "projects",
    }

    state.mark_skipped("projects", 1, "duplicate", target_id=202, source_name="Project One")
    skipped_progress = get_progress(state, "projects", 1)
    assert skipped_progress.status == "completed"
    assert skipped_progress.error_message == "Skipped: duplicate"
    assert skipped_progress.target_id == 202

    state.create_source_mapping("inventories", 2, "Inventory Two")
    state.create_source_mapping("hosts", 3, "Host Three")

    assert state.has_source_mapping("inventories", 2) is True
    assert state.has_mapping("inventories", 2) is True
    assert state.get_source_mapping_count("inventories") == 1
    assert state.get_unmapped_count("inventories") == 1
    assert state.get_all_source_ids("hosts") == [3]
    assert state.get_max_exported_id("hosts") == 3

    partial = state.detect_partial_import()
    assert partial["inventories"]["pending"] == 1
    assert partial["inventories"]["total_imported"] == 0
    assert partial["hosts"]["pending"] == 1

    state.mark_completed("inventories", 2, 302, source_name="Inventory Two")
    assert state.count_mapped_resources("inventories") == 1
    assert state.get_target_ids_for_type("inventories") == [302]

    overall = state.get_migration_stats()
    assert overall["total"] == 2
    assert overall["completed"] == 2


def test_state_batch_helpers_and_reset_operations(sqlite_db_url):
    state = build_state(sqlite_db_url)

    assert state.batch_create_mappings([], batch_size=2) == 0

    processed = state.batch_create_mappings(
        [
            {
                "resource_type": "users",
                "source_id": 1,
                "target_id": None,
                "source_name": "alice",
            },
            {
                "resource_type": "users",
                "source_id": 2,
                "target_id": 2002,
                "source_name": "bob",
                "target_name": "bob-target",
            },
            {
                "resource_type": "users",
                "source_id": 2,
                "target_id": 2222,
                "source_name": "bob",
                "target_name": "bob-updated",
            },
        ],
        batch_size=2,
    )
    assert processed == 3
    assert state.get_source_mapping_count("users") == 2
    assert state.get_unmapped_count("users") == 1
    assert state.get_mapped_id("users", 2) == 2222
    assert state.get_mapping_by_name("users", "bob").target_name == "bob-updated"

    state.mark_in_progress("users", 1, "alice")
    state.mark_completed("users", 1, 1001, target_name="alice-target")
    state.mark_in_progress("users", 2, "bob")
    state.mark_completed("users", 2, 2222, target_name="bob-updated")

    assert state.reset_target_ids("users") == 2
    assert state.get_target_ids_for_type("users") == []
    assert state.get_unmapped_count("users") == 2
    assert get_progress(state, "users", 1).status == "pending"
    assert get_mapping(state, "users", 1).target_id is None

    state.mark_in_progress("projects", 9, "proj-9")
    state.mark_completed("projects", 9, 9009, target_name="proj-9")
    assert state.clear_progress("projects") == 1
    assert get_progress(state, "projects", 9) is None
    assert get_mapping(state, "projects", 9) is None

    state.mark_export_failed("job_templates", 7, "JT Seven", "export failed")
    export_failed = get_progress(state, "job_templates", 7)
    assert export_failed.status == "failed"
    assert export_failed.phase == "export"

    state.mark_transform_skipped("workflow_job_templates", 8, "WF Eight", "missing dependency")
    transform_skipped = get_progress(state, "workflow_job_templates", 8)
    assert transform_skipped.status == "skipped"
    assert transform_skipped.phase == "transform"


def test_state_exports_and_imports_round_trip(sqlite_db_url, tmp_path):
    source_state = build_state(sqlite_db_url, migration_id="source-migration")
    source_state.mark_in_progress("teams", 11, "Team Eleven")
    source_state.mark_completed("teams", 11, 1111, target_name="Team Eleven Target")
    source_state.create_or_update_mapping("organizations", 12, None, source_name="Org Twelve")
    source_state.mark_transform_skipped("credentials", 13, "Cred Thirteen", "missing org")

    export_path = tmp_path / "state.json"
    source_state.export_state(str(export_path))

    exported = json.loads(export_path.read_text())
    assert exported["migration_id"] == "source-migration"
    assert exported["stats"]["completed"] == 1
    assert any(item["resource_type"] == "teams" for item in exported["id_mappings"])

    imported_db_url = f"sqlite:///{tmp_path / 'imported.db'}"
    imported_state = build_state(imported_db_url, migration_id="imported-migration")
    imported_state.import_state(str(export_path))

    assert imported_state.get_mapped_id("teams", 11) == 1111
    assert imported_state.get_status("credentials", 13) == "skipped"
    assert imported_state.get_source_mapping_count("organizations") == 1
    assert imported_state.get_unmapped_count("organizations") == 1


def test_state_covers_seed_and_update_branches(sqlite_db_url, tmp_path):
    state = build_state(sqlite_db_url)

    with pytest.raises(StateError, match="Cannot mark as completed"):
        state.mark_completed("teams", 20, 2000)

    state.mark_completed("teams", 20, 2000, source_name="Team Twenty", target_name="Team Twenty")
    seeded_progress = get_progress(state, "teams", 20)
    assert seeded_progress.status == "completed"
    assert seeded_progress.phase == "transform"
    assert state.get_mapping_by_name("teams", "Team Twenty").target_id == 2000

    with pytest.raises(StateError, match="Cannot mark as failed"):
        state.mark_failed("teams", 999, "missing")

    state.mark_failed("teams", 20, "soft failure", increment_retry=False)
    failed_progress = get_progress(state, "teams", 20)
    assert failed_progress.retry_count == 0
    assert failed_progress.status == "failed"

    state.mark_export_failed("teams", 20, "Team Twenty", "export retry")
    export_failed = get_progress(state, "teams", 20)
    assert export_failed.phase == "export"
    assert export_failed.error_message == "export retry"

    state.mark_transform_skipped("teams", 20, "Team Twenty", "dependency missing")
    transform_skipped = get_progress(state, "teams", 20)
    assert transform_skipped.phase == "transform"
    assert transform_skipped.error_message == "Transform skipped: dependency missing"

    with pytest.raises(StateError, match="Cannot mark as skipped"):
        state.mark_skipped("users", 21, "no source name")

    state.mark_skipped(
        "users",
        21,
        "duplicate",
        target_id=2100,
        target_name="User Twenty One",
        source_name="user21",
    )
    skipped_progress = get_progress(state, "users", 21)
    assert skipped_progress.status == "skipped"
    assert skipped_progress.target_id == 2100

    assert state.get_id_mapping("users", 21) is None
    assert state.get_mapping_by_name("users", "missing") is None

    state.save_id_mapping(
        "organizations",
        30,
        3000,
        source_name="Org Thirty",
        target_name="Org Thirty",
        mapping_metadata={"scope": "initial"},
    )
    first_mapping = get_mapping(state, "organizations", 30)
    assert first_mapping.mapping_metadata == {"scope": "initial"}

    state.save_id_mapping(
        "organizations",
        30,
        3333,
        source_name="Org Thirty Updated",
        target_name="Org Thirty Updated",
        mapping_metadata={"scope": "updated"},
    )
    updated_mapping = get_mapping(state, "organizations", 30)
    assert updated_mapping.target_id == 3333
    assert updated_mapping.mapping_metadata == {"scope": "updated"}

    state.create_or_update_mapping("organizations", 30, None, source_name="Org Thirty Final")
    preserved_mapping = get_mapping(state, "organizations", 30)
    assert preserved_mapping.target_id == 3333
    assert preserved_mapping.source_name == "Org Thirty Final"

    import_path = tmp_path / "import-update.json"
    import_path.write_text(
        json.dumps(
            {
                "progress": [
                    {
                        "resource_type": "teams",
                        "source_id": 20,
                        "source_name": "Team Twenty",
                        "target_id": 2222,
                        "status": "completed",
                        "phase": "import",
                        "retry_count": 2,
                        "error_message": None,
                    }
                ],
                "id_mappings": [
                    {
                        "resource_type": "organizations",
                        "source_id": 30,
                        "target_id": 4444,
                        "source_name": "Org Thirty Imported",
                        "target_name": "Org Thirty Imported",
                    },
                    {
                        "resource_type": "projects",
                        "source_id": 31,
                        "target_id": 3100,
                        "source_name": "Proj Thirty One",
                        "target_name": "Proj Thirty One",
                    },
                ],
            }
        )
    )
    state.import_state(str(import_path))

    imported_progress = get_progress(state, "teams", 20)
    assert imported_progress.status == "completed"
    assert imported_progress.phase == "import"
    assert imported_progress.retry_count == 2
    assert imported_progress.target_id == 2222
    assert state.get_mapped_id("organizations", 30) == 4444
    assert state.get_id_mapping("projects", 31)["target_name"] == "Proj Thirty One"
