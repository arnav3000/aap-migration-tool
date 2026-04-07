#!/usr/bin/env python3
"""
Sync inventory sources in target AAP and wait for completion.
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.migration.state import MigrationState
from aap_migration.config import StateConfig, AAPInstanceConfig


async def sync_inventory_source(client: AAPTargetClient, source_id: int, source_name: str) -> bool:
    """Trigger inventory source sync and wait for completion."""
    print(f"\n🔄 Syncing inventory source: {source_name} (ID: {source_id})")

    try:
        # Trigger the sync/update (use relative path without base URL)
        update_url = f"inventory_sources/{source_id}/update/"
        print(f"   Triggering sync: POST {update_url}")
        response = await client.post(update_url, data={})

        # The response from the POST is the created inventory_update object
        if response and isinstance(response, dict):
            print(f"   ✅ Sync triggered successfully (inventory_update: {response.get('id')})")

            # Poll for completion
            max_wait = 180  # 3 minutes max
            poll_interval = 5  # Check every 5 seconds
            elapsed = 0

            while elapsed < max_wait:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                # Get inventory source status
                source = await client.get(f"inventory_sources/{source_id}/")
                status = source.get("status")

                print(f"   Status: {status} (waited {elapsed}s)")

                if status == "successful":
                    print(f"   ✅ Sync completed successfully!")
                    return True
                elif status == "failed":
                    print(f"   ❌ Sync failed")
                    return False
                elif status in ["running", "pending", "waiting"]:
                    continue
                else:
                    print(f"   ⚠️  Unknown status: {status}")

            print(f"   ⏱️  Timeout waiting for sync (waited {max_wait}s)")
            return False
        else:
            print(f"   ❌ Failed to trigger sync")
            return False

    except Exception as e:
        print(f"   ❌ Error syncing: {e}")
        return False


async def verify_hosts(client: AAPTargetClient, inventory_id: int, inventory_name: str, expected_hosts: int):
    """Verify hosts were populated in inventory."""
    print(f"\n🔍 Verifying hosts in inventory: {inventory_name} (ID: {inventory_id})")

    try:
        inventory = await client.get(f"inventories/{inventory_id}/")
        total_hosts = inventory.get("total_hosts", 0)

        print(f"   Total hosts: {total_hosts} (expected: {expected_hosts})")

        if total_hosts >= expected_hosts:
            print(f"   ✅ Hosts verified!")
            return True
        else:
            print(f"   ⚠️  Host count mismatch")
            return False

    except Exception as e:
        print(f"   ❌ Error verifying: {e}")
        return False


async def main():
    """Main sync workflow."""
    print("=" * 80)
    print("🚀 Inventory Source Sync")
    print("=" * 80)

    # Get config from environment
    target_url = os.getenv("TARGET__URL")
    target_token = os.getenv("TARGET__TOKEN")
    db_path_str = os.getenv("MIGRATION_STATE_DB_PATH", "sqlite:///./migration_state.db")

    if not target_url or not target_token:
        print("❌ TARGET__URL and TARGET__TOKEN environment variables must be set")
        return 1

    # Initialize state to get mappings
    state_config = StateConfig(db_path=db_path_str)
    state = MigrationState(state_config)

    # Get inventory source target IDs from database
    target_ids = state.get_target_ids_for_type("inventory_sources")

    if not target_ids:
        print("❌ No inventory sources found in migration database")
        return 1

    # Get full mapping details using get_id_mapping
    inventory_sources = []
    for target_id in target_ids:
        # We need source_id to query the mapping, let's query the database directly
        import sqlite3
        conn = sqlite3.connect(db_path_str.replace("sqlite:///", ""))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT source_id, target_id, source_name
            FROM id_mappings
            WHERE resource_type = 'inventory_sources' AND target_id = ?
        """, (target_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            inventory_sources.append({
                'source_id': row[0],
                'target_id': row[1],
                'source_name': row[2]
            })

    print(f"\n📋 Found {len(inventory_sources)} inventory sources to sync:")
    for mapping in inventory_sources:
        print(f"   - {mapping['source_name']} (source:{mapping['source_id']} → target:{mapping['target_id']})")

    # Initialize target client
    target_config = AAPInstanceConfig(
        url=target_url,
        token=target_token,
        verify_ssl=False,
    )

    async with AAPTargetClient(config=target_config) as client:

        results = []

        # Sync each inventory source
        for mapping in inventory_sources:
            target_id = mapping['target_id']
            source_name = mapping['source_name']

            success = await sync_inventory_source(client, target_id, source_name)
            results.append((source_name, success))

        # Get inventory mappings for verification
        inventory_mappings = {}
        import sqlite3
        conn = sqlite3.connect(db_path_str.replace("sqlite:///", ""))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT source_id, target_id, source_name
            FROM id_mappings
            WHERE resource_type = 'inventories' AND target_id IS NOT NULL
        """)
        for row in cursor.fetchall():
            inventory_mappings[row[0]] = {
                'target_id': row[1],
                'name': row[2]
            }
        conn.close()

        # Verify host counts
        # Inventory 8 (Dynamic SCM Inventory) should have 4 hosts
        if 8 in inventory_mappings:
            inv_target_id = inventory_mappings[8]['target_id']
            inv_name = inventory_mappings[8]['name']
            await verify_hosts(client, inv_target_id, inv_name, expected_hosts=4)

        # Inventory 10 (Project File Inventory) should have 1 host
        if 10 in inventory_mappings:
            inv_target_id = inventory_mappings[10]['target_id']
            inv_name = inventory_mappings[10]['name']
            await verify_hosts(client, inv_target_id, inv_name, expected_hosts=1)

        # Summary
        print("\n" + "=" * 80)
        print("📊 Sync Summary")
        print("=" * 80)

        success_count = sum(1 for _, success in results if success)
        total_count = len(results)

        for name, success in results:
            status = "✅ SUCCESS" if success else "❌ FAILED"
            print(f"   {status}: {name}")

        print(f"\n✨ Overall: {success_count}/{total_count} inventory sources synced successfully")

        if success_count == total_count:
            print("\n🎉 All inventory sources synced! Dynamic hosts should now be populated.")
            return 0
        else:
            print("\n⚠️  Some syncs failed. Check logs above for details.")
            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
