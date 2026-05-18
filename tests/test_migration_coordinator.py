from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aap_migration.config import MigrationConfig
from aap_migration.migration.coordinator import MigrationCoordinator
from aap_migration.migration.transformer import SkipResourceError
from aap_migration.schema.models import Severity


class FakeState:
    def __init__(self):
        self.migration_id = "migration-123"
        self.database_url = "sqlite:///fake.db"
        self.migrated = set()
        self.mapped_ids = {}

    def is_migrated(self, resource_type, source_id):
        return (resource_type, source_id) in self.migrated

    def get_mapped_id(self, resource_type, source_id):
        return self.mapped_ids.get((resource_type, source_id))


class FakeCheckpointManager:
    def __init__(self):
        self.created = []
        self.restored = {}

    def create_checkpoint(self, phase, description, progress_stats):
        self.created.append((phase, description, dict(progress_stats)))
        return len(self.created)

    def restore_checkpoint(self, checkpoint_id):
        return self.restored.get(checkpoint_id)


class FakeProgressTracker:
    def __init__(self):
        self.updates = []
        self.started = []
        self.completed = 0
        self.closed = False

    def start_phase(self, name):
        self.started.append(name)

    def complete_phase(self):
        self.completed += 1

    def update_resource(self, **kwargs):
        self.updates.append(kwargs)

    def close(self):
        self.closed = True


class FakePhaseProgress:
    def __init__(self):
        self.calls = []

    def update(self, task_id, total=None):
        self.calls.append((task_id, total))


class FakeProgressDisplay:
    def __init__(self):
        self.phase_states = {}
        self.phase_tasks = {}
        self.phase_progress = FakePhaseProgress()
        self.updated = []
        self.completed = []
        self.total_phases = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_total_phases(self, total):
        self.total_phases = total

    def start_phase(self, phase_name, resource_type, total_items):
        phase_id = f"{phase_name}-id"
        self.phase_states[phase_id] = SimpleNamespace(total_items=total_items)
        self.phase_tasks[phase_id] = phase_id
        return phase_id

    def complete_phase(self, phase_id):
        self.completed.append(phase_id)

    def update_phase(self, phase_id, completed, failed, skipped):
        self.updated.append((phase_id, completed, failed, skipped))


def build_config(dry_run=False, skip_validation=False):
    return MigrationConfig(
        source={"url": "https://source.example.com", "token": "src-token"},
        target={"url": "https://target.example.com", "token": "dst-token"},
        dry_run=dry_run,
        skip_validation=skip_validation,
    )


def build_coordinator(dry_run=False, skip_validation=False):
    state = FakeState()
    coordinator = MigrationCoordinator(
        config=build_config(dry_run=dry_run, skip_validation=skip_validation),
        source_client=object(),
        target_client=object(),
        state=state,
        enable_progress=False,
    )
    coordinator.checkpoint_manager = FakeCheckpointManager()
    return coordinator, state


@pytest.mark.asyncio
async def test_coordinator_precheck_summary_schema_and_resume(tmp_path, monkeypatch):
    coordinator, _state = build_coordinator()

    missing_diff = SimpleNamespace(
        source_id=10,
        name="Vault",
        credential_type_name="HashiCorp Vault",
        organization_name="Default",
    )
    fake_result = SimpleNamespace(
        total_source=5,
        total_target=3,
        matching_credentials=2,
        missing_in_target=[missing_diff],
        managed_credentials_skipped=1,
    )

    class FakeComparator:
        def __init__(self, source_client, target_client, state):
            self.source_client = source_client
            self.target_client = target_client
            self.state = state

        async def compare_credentials(self):
            return fake_result

        def generate_report(self, result):
            assert result is fake_result
            return "# Credential Report\n"

    monkeypatch.setattr("aap_migration.migration.coordinator.CredentialComparator", FakeComparator)

    report_path = tmp_path / "reports" / "credential.md"
    summary = await coordinator.compare_and_verify_credentials(str(report_path))
    assert summary["missing_count"] == 1
    assert summary["missing_credentials"][0]["name"] == "Vault"
    assert report_path.read_text() == "# Credential Report\n"

    selected = coordinator._determine_phases(skip_phases=["credentials"], only_phases=None)
    assert all(phase["name"] != "credentials" for phase in selected)
    only = coordinator._determine_phases(skip_phases=None, only_phases=["credentials"])
    assert [phase["name"] for phase in only] == ["credentials"]
    with pytest.raises(ValueError):
        coordinator._determine_phases(skip_phases=["a"], only_phases=["b"])

    start = datetime.now(UTC)
    coordinator.metrics.update(
        {
            "start_time": start,
            "end_time": start + timedelta(seconds=12),
            "phases_completed": 3,
            "phases_failed": 1,
            "total_resources_exported": 9,
            "total_resources_imported": 7,
            "total_resources_failed": 2,
            "total_resources_skipped": 1,
            "errors": [{"phase": "projects", "error": "boom"}],
            "skipped_items": [{"resource_type": "hosts"}],
        }
    )
    generated = coordinator._generate_summary()
    assert generated["status"] == "completed_with_errors"
    assert generated["duration_seconds"] == 12

    async def fake_fetch_schema(client, resource_type):
        return {"resource_type": resource_type, "client": client}

    def fake_compare(resource_type, source_schema, target_schema):
        if resource_type == "users":
            return SimpleNamespace(
                has_breaking_changes=True,
                field_diffs=[SimpleNamespace(is_breaking=True, severity=Severity.CRITICAL)],
                schema_changes=[],
            )
        raise RuntimeError("compare failed")

    coordinator.schema_comparator = SimpleNamespace(
        fetch_schema=fake_fetch_schema,
        compare_schemas=fake_compare,
    )
    comparisons = await coordinator.compare_schemas_before_migration(["users", "teams"])
    assert list(comparisons.keys()) == ["users"]
    assert coordinator.has_critical_schema_issues() is True

    coordinator.checkpoint_manager.restored[4] = {"phase": "credentials"}
    resumed = {}

    async def fake_migrate_all(**kwargs):
        resumed.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(coordinator, "migrate_all", fake_migrate_all)
    assert await coordinator.resume_from_checkpoint(4) == {"status": "ok"}
    assert resumed["only_phases"][0] == "credential_input_sources"


@pytest.mark.asyncio
async def test_coordinator_execute_phase_and_regular_etl_pipeline(monkeypatch):
    coordinator, state = build_coordinator()
    coordinator.progress_tracker = FakeProgressTracker()
    coordinator.progress_display = FakeProgressDisplay()
    coordinator._current_phase_id = coordinator.progress_display.start_phase(
        "identity", "Identity", total_items=100
    )

    class FakeExporter:
        async def export(self):
            for item in [
                {"id": 1, "name": "good"},
                {"id": 2, "name": "skip"},
                {"id": 3, "name": "already-imported"},
                {"id": 4, "name": "import-fail"},
                {"id": 5, "name": "transform-error"},
            ]:
                yield dict(item)

    class FakeTransformer:
        def transform_resource(self, resource_type, data, validate=True):
            if data["name"] == "skip":
                raise SkipResourceError(
                    "missing dependency",
                    resource_type=resource_type,
                    source_id=data["_source_id"],
                    missing_dependency="teams:10",
                )
            if data["name"] == "transform-error":
                raise RuntimeError("transform exploded")
            transformed = dict(data)
            transformed["transformed"] = True
            return transformed

    class FakeImporter:
        def __init__(self):
            self.import_errors = [{"source_id": 4, "error": "import failed"}]

        async def import_resource(self, resource_type, source_id, data):
            if source_id == 1:
                return True
            if source_id == 3:
                state.migrated.add((resource_type, source_id))
                return False
            return False

    monkeypatch.setattr(
        "aap_migration.migration.coordinator.create_exporter",
        lambda **kwargs: FakeExporter(),
    )
    monkeypatch.setattr(
        "aap_migration.migration.coordinator.create_transformer",
        lambda **kwargs: FakeTransformer(),
    )
    monkeypatch.setattr(
        "aap_migration.migration.coordinator.create_importer",
        lambda **kwargs: FakeImporter(),
    )

    stats = await coordinator._execute_etl_pipeline("users", {"name": "identity"})
    assert stats == {
        "exported": 5,
        "transformed": 3,
        "imported": 1,
        "skipped": 2,
        "failed": 2,
    }
    assert coordinator.metrics["errors"][0]["resource_type"] == "users"
    assert coordinator.metrics["skipped_items"][0]["missing_dependency"] == "teams:10"
    assert coordinator.progress_display.updated[-1] == ("identity-id", 3, 2, 2)

    async def fake_pipeline(resource_type, phase_config):
        return {"exported": 2, "transformed": 2, "imported": 1, "skipped": 1, "failed": 0}

    monkeypatch.setattr(coordinator, "_execute_etl_pipeline", fake_pipeline)
    await coordinator._execute_phase(
        {"name": "identity", "description": "Identity", "resource_types": ["users", "teams"]}
    )
    assert coordinator.metrics["total_resources_exported"] == 4
    assert coordinator.checkpoint_manager.created[0][0] == "identity"


@pytest.mark.asyncio
async def test_coordinator_bulk_host_migration_and_dry_run_progress():
    coordinator, state = build_coordinator()
    coordinator.progress_tracker = FakeProgressTracker()
    coordinator.progress_display = FakeProgressDisplay()
    coordinator._current_phase_id = coordinator.progress_display.start_phase(
        "hosts", "Hosts", total_items=100
    )
    state.mapped_ids[("inventories", 1)] = 101

    class FakeHostExporter:
        async def export(self):
            for item in [
                {"id": 1, "name": "no-inventory"},
                {"id": 2, "name": "skip", "inventory": 1},
                {"id": 3, "name": "no-target-map", "inventory": 2},
                {"id": 4, "name": "explode", "inventory": 1},
                {"id": 5, "name": "good-a", "inventory": 1},
                {"id": 6, "name": "good-b", "inventory": 1},
            ]:
                yield dict(item)

    class FakeHostTransformer:
        def transform_resource(self, resource_type, data, validate=False):
            if data["name"] == "skip":
                raise SkipResourceError(
                    "missing inventory",
                    resource_type="hosts",
                    source_id=data["_source_id"],
                    missing_dependency="inventories:1",
                )
            if data["name"] == "explode":
                raise RuntimeError("boom")
            transformed = dict(data)
            transformed["transformed"] = True
            return transformed

    class FakeHostImporter:
        async def import_hosts_bulk(self, inventory_id, hosts):
            assert inventory_id == 101
            assert [host["name"] for host in hosts] == ["good-a", "good-b"]
            return {"total_created": 1, "total_failed": 1, "total_skipped": 0}

    stats = await coordinator._execute_bulk_host_migration(
        FakeHostExporter(),
        FakeHostTransformer(),
        FakeHostImporter(),
    )
    assert stats == {
        "exported": 6,
        "transformed": 3,
        "imported": 1,
        "skipped": 1,
        "failed": 4,
    }
    assert coordinator.metrics["skipped_items"][0]["resource_type"] == "hosts"
    assert coordinator.progress_display.updated[-1] == ("hosts-id", 5, 4, 1)

    dry_run_coordinator, dry_state = build_coordinator(dry_run=True)
    dry_run_coordinator.progress_tracker = FakeProgressTracker()
    dry_state.mapped_ids[("inventories", 1)] = 202

    class DryRunExporter:
        async def export(self):
            yield {"id": 9, "name": "dry", "inventory": 1}

    class DryRunTransformer:
        def transform_resource(self, resource_type, data, validate=False):
            return dict(data)

    class DryRunImporter:
        async def import_hosts_bulk(self, inventory_id, hosts):
            raise AssertionError("bulk import should not run during dry-run")

    dry_stats = await dry_run_coordinator._execute_bulk_host_migration(
        DryRunExporter(),
        DryRunTransformer(),
        DryRunImporter(),
    )
    assert dry_stats["imported"] == 1
