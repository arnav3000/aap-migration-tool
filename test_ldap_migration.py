#!/usr/bin/env python3
"""
Test LDAP Gateway Migration Implementation
"""
import asyncio
import json
import os
from pathlib import Path

# Load .env file
from dotenv import load_dotenv
load_dotenv()

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).parent / "src"))

from aap_migration.client.aap_target_client import AAPTargetClient
from aap_migration.config import AAPInstanceConfig, PerformanceConfig, StateConfig
from aap_migration.migration.state import MigrationState
from aap_migration.migration.importer import SettingsImporter


async def test_ldap_migration():
    """Test LDAP Gateway migration"""

    # Load configuration
    target_config = AAPInstanceConfig(
        url=os.getenv("TARGET__URL"),
        token=os.getenv("TARGET__TOKEN"),
        verify_ssl=False,
        timeout=30
    )

    perf_config = PerformanceConfig()
    state_config = StateConfig(db_path="migration_state.db")

    # Initialize client and state
    client = AAPTargetClient(target_config)
    state = MigrationState(state_config)

    # Create importer
    importer = SettingsImporter(client, state, perf_config)

    # Load transformed settings
    settings_file = Path("xformed/settings/settings_0001.json")
    with open(settings_file) as f:
        settings_data = json.load(f)

    settings = settings_data[0]

    print("=" * 70)
    print("LDAP GATEWAY MIGRATION TEST")
    print("=" * 70)

    # Check target version
    version = await client.get_version()
    print(f"\n✓ Target AAP Version: {version}")

    # Check current Gateway authenticators
    print("\n📊 Current Gateway Authenticators:")
    authenticators = await client.list_gateway_authenticators()
    for auth in authenticators:
        print(f"  - {auth['name']} (order: {auth['order']}, type: {auth['type']})")

    # Check LDAP settings in transformed data
    safe = settings.get('safe_to_copy', {})
    review = settings.get('review_required', {})
    sensitive = settings.get('sensitive', {})

    ldap_in_safe = [k for k in safe.keys() if k.startswith('AUTH_LDAP_')]
    ldap_in_review = [k for k in review.keys() if k.startswith('AUTH_LDAP_')]
    ldap_in_sensitive = [k for k in sensitive.keys() if k.startswith('AUTH_LDAP_')]

    print(f"\n📋 LDAP Settings Found:")
    print(f"  - In safe_to_copy: {len(ldap_in_safe)}")
    print(f"  - In review_required: {len(ldap_in_review)}")
    print(f"  - In sensitive: {len(ldap_in_sensitive)}")

    # Show specific LDAP servers detected
    primary_uri = review.get('AUTH_LDAP_SERVER_URI', {}).get('source_value')
    secondary_uri = review.get('AUTH_LDAP_1_SERVER_URI', {}).get('source_value')
    tertiary_uri = review.get('AUTH_LDAP_2_SERVER_URI', {}).get('source_value')

    print(f"\n🔍 LDAP Servers Detected:")
    if primary_uri:
        print(f"  ✓ Primary LDAP: {primary_uri}")
    if secondary_uri:
        print(f"  ✓ Secondary LDAP: {secondary_uri}")
    if tertiary_uri:
        print(f"  ✓ Tertiary LDAP: {tertiary_uri}")

    # Run import
    print(f"\n🚀 Running Settings Import...")
    result = await importer.import_resource(
        resource_type="settings",
        source_id=0,
        data=settings,
        resolve_dependencies=False
    )

    print(f"\n✅ Import Result:")
    print(json.dumps(result, indent=2))

    # Check Gateway authenticators after import
    print(f"\n📊 Gateway Authenticators After Import:")
    authenticators_after = await client.list_gateway_authenticators()
    for auth in authenticators_after:
        print(f"  - {auth['name']} (order: {auth['order']}, enabled: {auth['enabled']})")
        if 'LDAP' in auth['name']:
            config = auth.get('configuration', {})
            print(f"    SERVER_URI: {config.get('SERVER_URI')}")
            print(f"    BIND_DN: {config.get('BIND_DN')}")
            print(f"    USER_SEARCH: {config.get('USER_SEARCH')}")

    # Check if report was generated
    report_path = Path("SETTINGS-REVIEW-REPORT.md")
    if report_path.exists():
        print(f"\n📄 Settings Review Report Generated:")
        with open(report_path) as f:
            lines = f.readlines()
            for i, line in enumerate(lines[:20]):
                print(f"  {line.rstrip()}")
            if len(lines) > 20:
                print(f"  ... ({len(lines) - 20} more lines)")

    await client.close()
    print(f"\n{'=' * 70}")
    print("TEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(test_ldap_migration())
