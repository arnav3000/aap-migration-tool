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


class TestHostGroupMembershipExporter:
    @pytest.mark.anyio
    async def test_export_yields_group_host_pairs(self):
        with patch("requests.Session"):
            from aap_migration.migration.exporter import HostGroupMembershipExporter

            mock_client = MagicMock()
            mock_state = MagicMock()
            mock_perf = MagicMock()

            exporter = HostGroupMembershipExporter(mock_client, mock_state, mock_perf)

            fake_groups = [
                {"id": 1, "name": "webservers", "inventory": 10},
                {"id": 2, "name": "dbservers", "inventory": 10},
            ]
            fake_hosts_by_group = {
                1: [{"id": 101, "name": "web1"}, {"id": 102, "name": "web2"}],
                2: [{"id": 201, "name": "db1"}],
            }

            async def fake_export_resources(resource_type, endpoint, page_size=200):
                if endpoint == "groups/":
                    for g in fake_groups:
                        yield g
                elif endpoint.startswith("groups/") and endpoint.endswith("/hosts/"):
                    group_id = int(endpoint.split("/")[1])
                    for h in fake_hosts_by_group.get(group_id, []):
                        yield h

            exporter.export_resources = fake_export_resources

            results = await _collect(exporter.export(filters=None))

        assert len(results) == 3
        group_ids = {r["group_id"] for r in results}
        host_ids = {r["host_id"] for r in results}
        assert group_ids == {1, 2}
        assert host_ids == {101, 102, 201}
        assert all("inventory_id" in r for r in results)

    @pytest.mark.anyio
    async def test_export_parallel_delegates_to_export(self):
        with patch("requests.Session"):
            from aap_migration.migration.exporter import HostGroupMembershipExporter

            exporter = HostGroupMembershipExporter(MagicMock(), MagicMock(), MagicMock())

            fake_records = [{"group_id": 1, "host_id": 101}]

            async def fake_export(filters=None):
                for r in fake_records:
                    yield r

            exporter.export = fake_export

            results = await _collect(exporter.export_parallel(
                resource_type="host_group_memberships",
                endpoint="",
                page_size=200,
                max_concurrent_pages=5,
            ))

        assert len(results) == 1

    def test_host_group_memberships_in_resource_registry(self):
        from aap_migration.resources import RESOURCE_REGISTRY
        assert "host_group_memberships" in RESOURCE_REGISTRY


class TestHostGroupMembershipImporter:
    @pytest.mark.anyio
    async def test_imports_host_into_group(self):
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostGroupMembershipImporter

            mock_client = AsyncMock()
            mock_state = MagicMock()
            mock_perf = MagicMock()
            mock_mappings = MagicMock()

            # Host 1 maps to target 101, group 10 maps to target 1010
            mock_state.is_migrated.return_value = False
            mock_state.get_mapped_id.side_effect = lambda rtype, sid: {
                ("inventory_groups", 10): 1010,
                ("hosts", 1): 101,
            }.get((rtype, sid))

            # No existing membership
            mock_client.get.return_value = {"count": 0, "results": []}

            importer = HostGroupMembershipImporter(
                mock_client, mock_state, mock_perf, mock_mappings
            )

            membership = {
                "group_id": 10,
                "host_id": 1,
                "group_name": "webservers",
                "host_name": "web1",
                "inventory_id": 100,
            }

            result = await importer.import_resource(membership)

        assert result["status"] == "created"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "groups/1010/hosts/" in call_args[0][0]
        assert call_args[1]["json_data"] == {"id": 101}

    @pytest.mark.anyio
    async def test_skips_if_already_migrated(self):
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostGroupMembershipImporter

            mock_client = AsyncMock()
            mock_state = MagicMock()
            mock_state.is_migrated.return_value = True

            importer = HostGroupMembershipImporter(
                mock_client, mock_state, MagicMock(), MagicMock()
            )

            result = await importer.import_resource({
                "group_id": 10, "host_id": 1,
                "group_name": "webservers", "host_name": "web1", "inventory_id": 100,
            })

        assert result["status"] == "skipped"
        mock_client.post.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_if_already_in_group(self):
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostGroupMembershipImporter

            mock_client = AsyncMock()
            mock_state = MagicMock()
            mock_state.is_migrated.return_value = False
            mock_state.get_mapped_id.side_effect = lambda rtype, sid: {
                ("inventory_groups", 10): 1010, ("hosts", 1): 101,
            }.get((rtype, sid))
            mock_client.get.return_value = {"count": 1, "results": [{"id": 101}]}

            importer = HostGroupMembershipImporter(
                mock_client, mock_state, MagicMock(), MagicMock()
            )

            result = await importer.import_resource({
                "group_id": 10, "host_id": 1,
                "group_name": "webservers", "host_name": "web1", "inventory_id": 100,
            })

        assert result["status"] == "skipped"
        mock_client.post.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_if_group_not_found_on_target(self):
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostGroupMembershipImporter

            mock_state = MagicMock()
            mock_state.is_migrated.return_value = False
            mock_state.get_mapped_id.return_value = None  # group not found

            importer = HostGroupMembershipImporter(
                AsyncMock(), mock_state, MagicMock(), MagicMock()
            )

            result = await importer.import_resource({
                "group_id": 10, "host_id": 1,
                "group_name": "webservers", "host_name": "web1", "inventory_id": 100,
            })

        assert result["status"] == "skipped"
        assert "group_not_found" in result["reason"]

    @pytest.mark.anyio
    async def test_bulk_method_processes_all_memberships(self):
        with patch("requests.Session"):
            from aap_migration.migration.importer import HostGroupMembershipImporter

            importer = HostGroupMembershipImporter(
                AsyncMock(), MagicMock(), MagicMock(), MagicMock()
            )

            calls = []

            async def fake_import_resource(resource, xformed=None):
                calls.append(resource)
                return {"status": "created"}

            importer.import_resource = fake_import_resource

            memberships = [
                {"group_id": 1, "host_id": 1},
                {"group_id": 1, "host_id": 2},
                {"group_id": 2, "host_id": 1},
            ]
            results = await importer.import_host_group_memberships(memberships)

        assert len(calls) == 3
        assert len(results) == 3
