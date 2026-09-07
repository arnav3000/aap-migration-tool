"""Unit tests for HostInventoryMembershipExporter.export_parallel override."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


async def _collect(agen):
    results = []
    async for item in agen:
        results.append(item)
    return results


class TestHostInventoryMembershipExporterParallel:
    @pytest.mark.anyio
    async def test_export_parallel_delegates_to_export(self):
        """export_parallel must call self.export(), not the base class endpoint."""
        with patch("requests.Session"):
            from aap_migration.migration.exporter import HostInventoryMembershipExporter
            from aap_migration.migration.state import MigrationState

            mock_client = MagicMock()
            mock_state = MagicMock(spec=MigrationState)
            mock_perf = MagicMock()

            exporter = HostInventoryMembershipExporter(mock_client, mock_state, mock_perf)

            fake_memberships = [
                {"host_id": 1, "inventory_id": 10, "host_name": "h1", "inventory_name": "inv1"},
                {"host_id": 2, "inventory_id": 10, "host_name": "h2", "inventory_name": "inv1"},
            ]

            async def fake_export(filters=None):
                for m in fake_memberships:
                    yield m

            exporter.export = fake_export

            results = await _collect(exporter.export_parallel(
                resource_type="host_inventory_memberships",
                endpoint="",
                page_size=200,
                max_concurrent_pages=5,
            ))

        assert len(results) == 2
        assert results[0]["host_id"] == 1
        assert results[1]["host_id"] == 2


class TestImportHostInventoryMemberships:
    @pytest.mark.anyio
    async def test_bulk_method_calls_import_resource_per_membership(self):
        """import_host_inventory_memberships must call import_resource for each record."""
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostInventoryMembershipImporter

            mock_client = MagicMock()
            mock_state = MagicMock()
            mock_perf = MagicMock()
            mock_mappings = MagicMock()

            importer = HostInventoryMembershipImporter(
                mock_client, mock_state, mock_perf, mock_mappings
            )

            memberships = [
                {"host_id": 1, "inventory_id": 10, "host_name": "h1", "inventory_name": "inv1"},
                {"host_id": 2, "inventory_id": 10, "host_name": "h2", "inventory_name": "inv1"},
            ]

            call_args = []

            async def fake_import_resource(resource, xformed=None):
                call_args.append(resource)
                return {"status": "created"}

            importer.import_resource = fake_import_resource

            results = await importer.import_host_inventory_memberships(memberships)

        assert len(call_args) == 2
        assert call_args[0]["host_id"] == 1
        assert call_args[1]["host_id"] == 2

    @pytest.mark.anyio
    async def test_empty_memberships_returns_empty_list(self):
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostInventoryMembershipImporter
            importer = HostInventoryMembershipImporter(
                MagicMock(), MagicMock(), MagicMock(), MagicMock()
            )
            results = await importer.import_host_inventory_memberships([])
        assert results == []


class TestHostInventoryMembershipWiring:
    def test_coordinator_has_host_memberships_phase(self):
        from aap_migration.migration.coordinator import MigrationCoordinator
        phase_names = [p["name"] for p in MigrationCoordinator.MIGRATION_PHASES]
        assert "host_memberships" in phase_names

    def test_host_memberships_phase_after_hosts(self):
        from aap_migration.migration.coordinator import MigrationCoordinator
        phase_names = [p["name"] for p in MigrationCoordinator.MIGRATION_PHASES]
        assert phase_names.index("host_memberships") > phase_names.index("hosts")

    def test_host_memberships_phase_contains_resource_type(self):
        from aap_migration.migration.coordinator import MigrationCoordinator
        phase = next(
            p for p in MigrationCoordinator.MIGRATION_PHASES
            if p["name"] == "host_memberships"
        )
        assert "host_inventory_memberships" in phase["resource_types"]
