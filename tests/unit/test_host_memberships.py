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
