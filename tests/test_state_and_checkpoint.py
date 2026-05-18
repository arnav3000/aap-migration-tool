from __future__ import annotations

import pytest

from aap_migration.client.exceptions import CheckpointError, StateError
from aap_migration.config import StateConfig
from aap_migration.migration.checkpoint import AutoCheckpointer, CheckpointManager
from aap_migration.migration.state import MigrationState


def build_state(tmp_path, *, migration_id: str = "migration-1") -> MigrationState:
    return MigrationState(
        StateConfig(db_path=str(tmp_path / f"{migration_id}.db")),
        migration_id=migration_id,
        migration_name="Example Migration",
    )


def test_migration_state_tracks_progress_and_mappings(tmp_path) -> None:
    state = build_state(tmp_path)

    assert state.database_url.startswith("sqlite:///")
    assert state.is_migrated("organizations", 10) is False

    state.mark_in_progress("organizations", 10, "Default")
    assert state.is_migrated("organizations", 10) is True

    state.mark_completed(
        "organizations",
        10,
        110,
        source_name="Default",
        target_name="Default Target",
    )

    assert state.get_mapped_id("organizations", 10) == 110
    assert state.has_mapping("organizations", 10) is True
    assert state.count_mapped_resources("organizations") == 1
    assert state.get_all_source_ids("organizations") == [10]
    assert state.get_id_mapping("organizations", 10) == {
        "source_id": 10,
        "target_id": 110,
        "source_name": "Default",
        "target_name": "Default Target",
        "resource_type": "organizations",
    }
    assert state.get_migration_stats("organizations") == {
        "total": 1,
        "pending": 0,
        "in_progress": 0,
        "completed": 1,
        "failed": 0,
        "skipped": 0,
    }


def test_mark_completed_requires_existing_record_or_source_name(tmp_path) -> None:
    state = build_state(tmp_path)

    with pytest.raises(StateError, match="Cannot mark as completed"):
        state.mark_completed("hosts", 55, 99)


def test_mark_failed_and_reset_failed(tmp_path) -> None:
    state = build_state(tmp_path)
    state.mark_in_progress("hosts", 20, "web01")
    state.mark_failed("hosts", 20, "boom")

    stats = state.get_migration_stats("hosts")
    assert stats["failed"] == 1

    reset_count = state.reset_failed("hosts")

    assert reset_count == 1
    assert state.get_migration_stats("hosts")["pending"] == 1


def test_checkpoint_manager_create_restore_and_resume_info(tmp_path) -> None:
    state = build_state(tmp_path)
    state.mark_in_progress("inventories", 1, "Inventory 1")
    state.mark_completed("inventories", 1, 101, source_name="Inventory 1")

    manager = CheckpointManager(state)
    checkpoint_id = manager.create_checkpoint(
        phase="inventories",
        checkpoint_data={"last_processed_id": 1},
        description="Checkpoint after first inventory",
    )

    restored = manager.restore_checkpoint(checkpoint_id)
    latest = manager.get_latest_checkpoint()
    resume = manager.get_resume_info()

    assert restored["phase"] == "inventories"
    assert restored["checkpoint_data"]["last_processed_id"] == 1
    assert latest is not None and latest["id"] == checkpoint_id
    assert resume is not None
    assert resume["checkpoint_id"] == checkpoint_id
    assert resume["completed"] == 1
    assert resume["percentage"] == 100.0


def test_checkpoint_manager_invalidates_and_rejects_restore(tmp_path) -> None:
    state = build_state(tmp_path)
    manager = CheckpointManager(state)
    checkpoint_id = manager.create_checkpoint(
        "projects", progress_stats={"total": 0, "completed": 0}
    )

    manager.invalidate_checkpoint(checkpoint_id, "corrupt")

    with pytest.raises(CheckpointError, match="marked as invalid"):
        manager.restore_checkpoint(checkpoint_id)


def test_auto_checkpointer_creates_checkpoint_at_frequency(tmp_path) -> None:
    state = build_state(tmp_path)
    state.mark_in_progress("teams", 1, "Team 1")
    state.mark_completed("teams", 1, 201, source_name="Team 1")

    manager = CheckpointManager(state)
    auto = AutoCheckpointer(manager, frequency=2)

    assert auto.track_completion("teams") is False
    assert auto.track_completion("teams", checkpoint_data={"batch": 2}) is True
    assert manager.has_checkpoints("teams") is True
