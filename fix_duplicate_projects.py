#!/usr/bin/env python3
"""
Find and map duplicate projects that already exist in target AAP.
Creates ID mappings so job templates can reference them.
"""
import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import sqlite3

# Load environment variables from .env
load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig


# Projects that failed to import due to duplicates
FAILED_PROJECTS = {
    8: "Web Application Deployment",
    9: "Infrastructure Configuration",
    10: "Database Management",
    11: "Security Hardening",
    12: "Application Monitoring",
}


async def find_and_map_projects():
    """Find duplicate projects in target AAP and create ID mappings."""
    print("=" * 80)
    print("🔍 Finding Duplicate Projects in Target AAP")
    print("=" * 80)

    # Get config from environment
    target_url = os.getenv("TARGET__URL")
    target_token = os.getenv("TARGET__TOKEN")
    db_path_str = os.getenv("MIGRATION_STATE_DB_PATH", "sqlite:///./migration_state.db")

    if not target_url or not target_token:
        print("❌ TARGET__URL and TARGET__TOKEN environment variables must be set")
        return 1

    # Initialize target client
    target_config = AAPInstanceConfig(
        url=target_url,
        token=target_token,
        verify_ssl=False,
    )

    async with AAPTargetClient(config=target_config) as client:

        print(f"\n📋 Searching for {len(FAILED_PROJECTS)} projects...")

        found_mappings = []
        not_found = []

        for source_id, project_name in FAILED_PROJECTS.items():
            print(f"\n🔎 Searching for: {project_name} (source ID: {source_id})")

            try:
                # Search for project by name
                results = await client.get("projects/", params={"name": project_name})

                if results and isinstance(results, dict) and results.get("count", 0) > 0:
                    projects = results.get("results", [])
                    if projects:
                        target_project = projects[0]
                        target_id = target_project["id"]
                        org_name = target_project.get("summary_fields", {}).get("organization", {}).get("name", "Unknown")

                        print(f"   ✅ Found in target AAP:")
                        print(f"      - Target ID: {target_id}")
                        print(f"      - Organization: {org_name}")
                        print(f"      - SCM Type: {target_project.get('scm_type', 'N/A')}")

                        found_mappings.append({
                            "source_id": source_id,
                            "target_id": target_id,
                            "name": project_name
                        })
                    else:
                        print(f"   ❌ Not found in target AAP")
                        not_found.append((source_id, project_name))
                else:
                    print(f"   ❌ Not found in target AAP")
                    not_found.append((source_id, project_name))

            except Exception as e:
                print(f"   ❌ Error searching: {e}")
                not_found.append((source_id, project_name))

        # Create ID mappings in database
        if found_mappings:
            print("\n" + "=" * 80)
            print("💾 Creating ID Mappings in Database")
            print("=" * 80)

            db_file = db_path_str.replace("sqlite:///", "")
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()

            for mapping in found_mappings:
                source_id = mapping["source_id"]
                target_id = mapping["target_id"]
                name = mapping["name"]

                # Check if mapping already exists
                cursor.execute("""
                    SELECT target_id FROM id_mappings
                    WHERE resource_type = 'projects' AND source_id = ?
                """, (source_id,))
                existing = cursor.fetchone()

                if existing:
                    if existing[0] is None:
                        # Update existing mapping with NULL target_id
                        cursor.execute("""
                            UPDATE id_mappings
                            SET target_id = ?, target_name = ?, source_name = ?
                            WHERE resource_type = 'projects' AND source_id = ?
                        """, (target_id, name, name, source_id))
                        print(f"   ✅ Updated mapping: {name} (source:{source_id} → target:{target_id})")
                    else:
                        print(f"   ℹ️  Mapping already exists: {name} (source:{source_id} → target:{existing[0]})")
                else:
                    # Create new mapping
                    cursor.execute("""
                        INSERT INTO id_mappings (resource_type, source_id, target_id, source_name, target_name)
                        VALUES ('projects', ?, ?, ?, ?)
                    """, (source_id, target_id, name, name))
                    print(f"   ✅ Created mapping: {name} (source:{source_id} → target:{target_id})")

            conn.commit()
            conn.close()

        # Summary
        print("\n" + "=" * 80)
        print("📊 Summary")
        print("=" * 80)
        print(f"   ✅ Found and mapped: {len(found_mappings)}/{len(FAILED_PROJECTS)}")

        if not_found:
            print(f"   ⚠️  Not found in target: {len(not_found)}")
            for source_id, name in not_found:
                print(f"      - {name} (source ID: {source_id})")

        if len(found_mappings) == len(FAILED_PROJECTS):
            print("\n🎉 All projects found and mapped! Ready to import job templates.")
            return 0
        elif found_mappings:
            print(f"\n⚠️  Partial success. {len(not_found)} projects need manual creation.")
            return 1
        else:
            print("\n❌ No projects found. They may have been deleted or have different names.")
            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(find_and_map_projects())
    sys.exit(exit_code)
