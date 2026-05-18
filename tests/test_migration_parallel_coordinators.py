import json

import pytest

from aap_migration.config import ExportConfig, PerformanceConfig
from aap_migration.migration.parallel_exporter import ParallelExportCoordinator
from aap_migration.migration.parallel_transformer import ParallelTransformCoordinator
from aap_migration.migration.transformer import SkipResourceError


class FakeMigrationState:
    def __init__(self, max_exported=None, source_mappings=None):
        self.max_exported = dict(max_exported or {})
        self.source_mappings = set(source_mappings or set())
        self.mapping_batches = []

    def batch_create_mappings(self, mappings, batch_size=100):
        self.mapping_batches.append((list(mappings), batch_size))

    def get_max_exported_id(self, resource_type):
        return self.max_exported.get(resource_type)

    def has_source_mapping(self, resource_type, source_id):
        return (resource_type, source_id) in self.source_mappings


class FakeExporter:
    def __init__(self, items):
        self.items = list(items)
        self.resume_checkpoint = None
        self.skip_dynamic_hosts = False
        self.skip_smart_inventories = False
        self.stats = {"skipped_count": 1}

    def set_skip_dynamic_hosts(self, value):
        self.skip_dynamic_hosts = value

    def set_skip_smart_inventories(self, value):
        self.skip_smart_inventories = value

    def set_resume_checkpoint(self, value):
        self.resume_checkpoint = value

    async def export_parallel(
        self,
        resource_type,
        endpoint,
        page_size=200,
        max_concurrent_pages=5,
        filters=None,
    ):
        for item in self.items:
            yield dict(item)

    def get_stats(self):
        return dict(self.stats)


class FakeTransformer:
    def __init__(self):
        self.stats = {"fields_removed": 2, "fields_added": 1, "fields_renamed": 3}
        self.populated = []

    def transform_resource(self, resource_type, resource):
        name = resource.get("name")
        if name == "skip-me":
            raise SkipResourceError(
                "missing dependency",
                resource_type=resource_type,
                source_id=resource["id"],
                missing_dependency="inventories:999",
            )
        if name == "explode":
            raise RuntimeError("transform exploded")
        transformed = dict(resource)
        transformed["transformed"] = True
        return transformed

    async def populate_target_id_from_target(self, data, target_client, state, source_id):
        self.populated.append((source_id, data.get("name")))
        data["_mapped_from_target"] = True
        return data


@pytest.mark.asyncio
async def test_parallel_export_coordinator_writes_batches_and_handles_resume(tmp_path, monkeypatch):
    exporter = FakeExporter(
        [
            {"id": 11, "name": "alpha"},
            {"id": 12, "name": "beta"},
        ]
    )
    state = FakeMigrationState(max_exported={"hosts": 10})
    progress_events = []

    monkeypatch.setattr(
        "aap_migration.migration.parallel_exporter.create_exporter",
        lambda *args, **kwargs: exporter,
    )
    monkeypatch.setattr(
        "aap_migration.migration.parallel_exporter.get_endpoint",
        lambda resource_type: f"{resource_type}/",
    )

    coordinator = ParallelExportCoordinator(
        source_client=object(),
        migration_state=state,
        performance_config=PerformanceConfig(),
        output_dir=tmp_path,
        records_per_file=1,
        export_config=ExportConfig(skip_dynamic_hosts=True),
    )

    stats = await coordinator.export_resource_type(
        "hosts",
        resume=True,
        progress_callback=lambda resource_type, payload: progress_events.append(
            (resource_type, payload["exported"])
        ),
    )

    assert stats["exported"] == 2
    assert stats["files_written"] == 2
    assert stats["skipped"] == 1
    assert exporter.resume_checkpoint == 10
    assert exporter.skip_dynamic_hosts is True
    assert progress_events == [("hosts", 1), ("hosts", 2)]
    assert len(state.mapping_batches) == 1

    first_file = json.loads((tmp_path / "hosts" / "hosts_0001.json").read_text())
    second_file = json.loads((tmp_path / "hosts" / "hosts_0002.json").read_text())
    assert first_file[0]["_source_id"] == 11
    assert second_file[0]["name"] == "beta"

    monkeypatch.setattr(
        "aap_migration.migration.parallel_exporter.create_exporter",
        lambda *args, **kwargs: (_ for _ in ()).throw(NotImplementedError("missing")),
    )
    skipped = await coordinator.export_resource_type("unknown_type")
    assert skipped["skip_reason"] == "No exporter implemented"


@pytest.mark.asyncio
async def test_parallel_export_coordinator_collects_parallel_results(tmp_path, monkeypatch):
    coordinator = ParallelExportCoordinator(
        source_client=object(),
        migration_state=FakeMigrationState(),
        performance_config=PerformanceConfig(max_concurrent_types=2),
        output_dir=tmp_path,
    )

    async def fake_export_resource_type(resource_type, resume=False, progress_callback=None):
        if resource_type == "broken":
            raise RuntimeError("boom")
        return {
            "resource_type": resource_type,
            "exported": 2,
            "failed": 0,
            "skipped": 1 if resource_type == "skipped" else 0,
            "skip_reason": "No exporter implemented" if resource_type == "skipped" else None,
        }

    monkeypatch.setattr(coordinator, "export_resource_type", fake_export_resource_type)

    results = await coordinator.export_all_parallel(["ok", "skipped", "broken"], resume=True)
    assert results["ok"]["exported"] == 2
    assert results["skipped"]["skip_reason"] == "No exporter implemented"
    assert coordinator.get_results() == results


@pytest.mark.asyncio
async def test_parallel_transform_coordinator_filters_hosts_and_populates_target_ids(
    tmp_path, monkeypatch
):
    input_dir = tmp_path / "raw"
    output_dir = tmp_path / "xformed"
    hosts_dir = input_dir / "hosts"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts_0001.json").write_text(
        json.dumps(
            [
                {"id": 1, "name": "already-skipped", "_skipped": True, "inventory": 1},
                {"id": 2, "name": "dynamic", "inventory": 1, "has_inventory_sources": True},
                {"id": 3, "name": "skip-me", "inventory": 1},
                {"id": 4, "name": "explode", "inventory": 1},
                {"id": 5, "name": "missing-inventory", "inventory": 99},
                {"id": 6, "name": "good", "inventory": 1},
            ]
        )
    )

    credential_types_dir = input_dir / "credential_types"
    credential_types_dir.mkdir(parents=True)
    (credential_types_dir / "credential_types_0001.json").write_text(
        json.dumps(
            [
                {
                    "id": 10,
                    "name": "skip-managed",
                    "managed": True,
                    "summary_fields": {"created_by": {}},
                },
                {"id": 11, "name": "map-me", "managed": True},
            ]
        )
    )

    transformers = {
        "hosts": FakeTransformer(),
        "credential_types": FakeTransformer(),
    }
    state = FakeMigrationState(source_mappings={("inventories", 1)})

    monkeypatch.setattr(
        "aap_migration.migration.parallel_transformer.create_transformer",
        lambda resource_type, **kwargs: transformers[resource_type],
    )

    progress_events = []
    coordinator = ParallelTransformCoordinator(
        migration_state=state,
        performance_config=PerformanceConfig(),
        input_dir=input_dir,
        output_dir=output_dir,
        target_client=object(),
    )

    host_stats = await coordinator.transform_resource_type(
        "hosts",
        progress_callback=lambda resource_type, payload: progress_events.append(
            (resource_type, payload["count"], payload["failed"])
        ),
    )
    assert host_stats["count"] == 2
    assert host_stats["failed"] == 1
    assert host_stats["skipped_dynamic_hosts"] == 1
    assert host_stats["skipped_from_transformer"] == 1
    assert host_stats["skipped_missing_inventory"] == 1
    assert host_stats["files"] == 1
    assert progress_events[-1] == ("hosts", 2, 1)

    host_output = json.loads((output_dir / "hosts" / "hosts_0001.json").read_text())
    assert [item["name"] for item in host_output] == ["good"]
    assert host_output[0]["_source_id"] == 6

    credential_stats = await coordinator.transform_resource_type("credential_types")
    assert credential_stats["count"] == 1
    assert credential_stats["skipped_custom_managed"] == 1
    assert transformers["credential_types"].populated == [(11, "map-me")]


@pytest.mark.asyncio
async def test_parallel_transform_coordinator_collects_results_and_missing_inputs(
    tmp_path, monkeypatch
):
    coordinator = ParallelTransformCoordinator(
        migration_state=FakeMigrationState(),
        performance_config=PerformanceConfig(max_concurrent_types=2),
        input_dir=tmp_path / "missing-raw",
        output_dir=tmp_path / "missing-xformed",
    )
    assert await coordinator.transform_resource_type("hosts") == {
        "resource_type": "hosts",
        "count": 0,
        "failed": 0,
        "files": 0,
        "skipped_pending_deletion": 0,
        "skipped_custom_managed": 0,
        "skipped_missing_inventory": 0,
        "skipped_smart_inventories": 0,
        "skipped_dynamic_hosts": 0,
        "skipped_from_transformer": 0,
        "fields_removed": 0,
        "fields_added": 0,
        "fields_renamed": 0,
    }

    async def fake_transform_resource_type(resource_type, progress_callback=None):
        if resource_type == "broken":
            raise RuntimeError("broken")
        return {"resource_type": resource_type, "count": 1}

    monkeypatch.setattr(coordinator, "transform_resource_type", fake_transform_resource_type)
    results = await coordinator.transform_all_parallel(["ok", "broken"])
    assert results == {"ok": {"resource_type": "ok", "count": 1}}
