#!/usr/bin/env python3
"""Simple test to verify job template schedules were migrated successfully."""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from aap_migration.client.aap_source_client import AAPSourceClient
from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig


async def compare_schedules():
    """Compare schedules between source and target AAP."""
    source_config = AAPInstanceConfig(
        url=os.getenv("SOURCE__URL"),
        token=os.getenv("SOURCE__TOKEN"),
        verify_ssl=False,
    )

    target_config = AAPInstanceConfig(
        url=os.getenv("TARGET__URL"),
        token=os.getenv("TARGET__TOKEN"),
        verify_ssl=False,
    )

    print("=" * 80)
    print("SCHEDULE MIGRATION VERIFICATION")
    print("=" * 80)

    # Test job templates (already imported)
    async with AAPSourceClient(config=source_config) as source_client, \
               AAPTargetClient(config=target_config) as target_client:

        # Get job templates with schedules from source
        source_response = await source_client.get("job_templates/")
        source_templates = source_response.get("results", [])

        source_schedules = {}
        for template in source_templates:
            sched_response = await source_client.get(f"job_templates/{template['id']}/schedules/")
            schedules = sched_response.get("results", [])
            if schedules:
                source_schedules[template['name']] = [s['name'] for s in schedules]

        # Get job templates with schedules from target
        target_response = await target_client.get("job_templates/")
        target_templates = target_response.get("results", [])

        target_schedules = {}
        for template in target_templates:
            sched_response = await target_client.get(f"job_templates/{template['id']}/schedules/")
            schedules = sched_response.get("results", [])
            if schedules:
                target_schedules[template['name']] = [s['name'] for s in schedules]

        print("\nJOB TEMPLATES SCHEDULES:")
        print("-" * 80)
        print(f"Source: {len(source_schedules)} templates with schedules")
        print(f"Target: {len(target_schedules)} templates with schedules")
        print()

        matched = 0
        missing = 0
        for template_name, schedules in source_schedules.items():
            if template_name in target_schedules:
                if set(schedules) == set(target_schedules[template_name]):
                    print(f"OK {template_name}: {len(schedules)} schedule(s)")
                    matched += 1
                else:
                    print(f"MISMATCH {template_name}:")
                    print(f"  Source: {schedules}")
                    print(f"  Target: {target_schedules[template_name]}")
                    missing += 1
            else:
                print(f"MISSING {template_name}: not found in target")
                missing += 1

        print()
        print("=" * 80)
        print(f"Result: {matched} matched, {missing} missing/mismatched")
        print("=" * 80)

        return 0 if missing == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(compare_schedules())
    exit(exit_code)
