#!/usr/bin/env python3
"""
Comprehensive test for schedule migration across all resource types.
Tests: job_templates, projects, inventory_sources, system_job_templates
"""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig


async def verify_all_schedules():
    """Verify schedules exist in target for all resource types."""
    config = AAPInstanceConfig(
        url=os.getenv("TARGET__URL"),
        token=os.getenv("TARGET__TOKEN"),
        verify_ssl=False,
    )

    print("=" * 80)
    print("COMPREHENSIVE SCHEDULE MIGRATION TEST")
    print("=" * 80)
    print()

    test_config = {
        "job_templates": {
            "endpoint": "job_templates",
            "expected": [
                "Configure Infrastructure",
                "MGMT - AAP Database Backup",
                "MGMT - Cleanup Old Job Data",
                "MGMT - System Health Check",
            ],
        },
        "projects": {
            "endpoint": "projects",
            "expected": [
                "Web Application Deployment",
                "Infrastructure Configuration",
                "Database Management",
                "Security Hardening",
                "Application Monitoring",
            ],
        },
        "inventory_sources": {
            "endpoint": "inventory_sources",
            "expected": [
                "Git Inventory Source",
                "Project Inventory File Source",
            ],
        },
        "system_job_templates": {
            "endpoint": "system_job_templates",
            "expected": [
                "Cleanup Activity Stream",
                "Cleanup Expired OAuth 2 Tokens",
                "Cleanup Expired Sessions",
                "Cleanup Job Details",
            ],
        },
    }

    async with AAPTargetClient(config=config) as client:
        total_expected = 0
        total_found = 0

        for resource_type, config_data in test_config.items():
            endpoint = config_data["endpoint"]
            expected_names = config_data["expected"]

            print(f"\n{resource_type.upper().replace('_', ' ')}")
            print("-" * 80)

            try:
                response = await client.get(f"{endpoint}/")
                resources = response.get("results", [])

                for expected_name in expected_names:
                    resource = next((r for r in resources if r["name"] == expected_name), None)

                    if resource:
                        resource_id = resource["id"]

                        # Get schedules
                        sched_response = await client.get(f"{endpoint}/{resource_id}/schedules/")
                        schedules = sched_response.get("results", [])

                        total_expected += 1  # Expect at least 1 schedule per resource

                        if schedules:
                            total_found += len(schedules)
                            print(f"OK {expected_name}")
                            for sched in schedules:
                                print(f"   - {sched['name']}")
                                print(f"     rrule: {sched.get('rrule', 'N/A')[:60]}...")
                        else:
                            print(f"FAIL {expected_name} - NO SCHEDULES FOUND")
                    else:
                        print(f"WARN {expected_name} - RESOURCE NOT FOUND IN TARGET")
                        total_expected += 1

            except Exception as e:
                print(f"ERROR checking {resource_type}: {e}")

        print()
        print("=" * 80)
        print(f"RESULTS: {total_found}/{total_expected} schedules found")
        print("=" * 80)

        if total_found == total_expected:
            print("SUCCESS: All schedules migrated correctly!")
            return 0
        else:
            print(f"INCOMPLETE: Expected {total_expected}, found {total_found}")
            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(verify_all_schedules())
    exit(exit_code)
