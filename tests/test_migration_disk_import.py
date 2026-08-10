"""Tests for disk-bound import aggregation (streaming multi-file shards)."""

from __future__ import annotations

import json

import pytest

from aap_migration.migration.pipeline import ETLStats
from aap_migration.migration.runner import run_disk_import


@pytest.mark.asyncio
async def test_run_disk_import_aggregates_hosts_shards(tmp_path, monkeypatch):
    """Import must load and merge all hosts_*.json shard files before importing."""
    xformed = tmp_path / "xformed"
    hosts_dir = xformed / "hosts"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts_0001.json").write_text(
        json.dumps(
            [
                {"_source_id": 1, "name": "host-a", "inventory": 10},
                {"_source_id": 2, "name": "host-b", "inventory": 10},
            ]
        )
    )
    (hosts_dir / "hosts_0002.json").write_text(
        json.dumps(
            [
                {"_source_id": 3, "name": "host-c", "inventory": 10},
            ]
        )
    )
    (xformed / "metadata.json").write_text(
        json.dumps(
            {
                "resource_types": {
                    "hosts": {"count": 3},
                }
            }
        )
    )

    captured: list[dict] = []

    async def fake_run_import_loop(resource_type, components, resources, state, **kwargs):
        assert resource_type == "hosts"
        captured.extend(resources)
        return ETLStats(imported=len(resources))

    async def fake_bootstrap(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "aap_migration.migration.pipeline.run_import_loop",
        fake_run_import_loop,
    )
    monkeypatch.setattr(
        "aap_migration.migration.pipeline.bootstrap_resource_type",
        fake_bootstrap,
    )

    class FakeState:
        def has_source_mapping(self, resource_type, source_id):
            return True

    result = await run_disk_import(
        source_client=object(),
        target_client=object(),
        state=FakeState(),
        input_dir=xformed,
        resource_types=["hosts"],
    )

    assert result["total_imported"] == 3
    assert {item["_source_id"] for item in captured} == {1, 2, 3}
    assert [item["name"] for item in captured] == ["host-a", "host-b", "host-c"]


@pytest.mark.asyncio
async def test_run_disk_import_skips_missing_types_and_applies_filters(tmp_path, monkeypatch):
    xformed = tmp_path / "xformed"
    orgs_dir = xformed / "organizations"
    orgs_dir.mkdir(parents=True)
    (orgs_dir / "organizations_0001.json").write_text(
        json.dumps(
            [
                {"id": 1, "name": "Org A", "organization": 1},
                {"id": 2, "name": "Org B", "organization": 2},
            ]
        )
    )
    (xformed / "metadata.json").write_text(
        json.dumps(
            {
                "resource_types": {
                    "organizations": {"count": 2},
                    "missing_importer": {"count": 1},
                }
            }
        )
    )

    logs: list[str] = []
    mappings_created: list[tuple] = []

    async def fake_run_import_loop(resource_type, components, resources, state, **kwargs):
        return ETLStats(imported=len(resources))

    async def fake_bootstrap(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "aap_migration.migration.pipeline.run_import_loop",
        fake_run_import_loop,
    )
    monkeypatch.setattr(
        "aap_migration.migration.pipeline.bootstrap_resource_type",
        fake_bootstrap,
    )

    class FakeState:
        def has_source_mapping(self, resource_type, source_id):
            return False

        def create_source_mapping(self, **kwargs):
            mappings_created.append(kwargs)

    result = await run_disk_import(
        source_client=object(),
        target_client=object(),
        state=FakeState(),
        input_dir=xformed,
        resource_types=["organizations", "missing_importer", "users"],
        org_ids=[1],
        name_prefix="test_",
        log=logs.append,
    )

    assert result["total_imported"] == 1
    assert result["resource_types"]["organizations"]["imported"] == 1
    assert any("no importer" in line for line in logs)
    assert mappings_created
    assert any("Import complete" in line for line in logs)
