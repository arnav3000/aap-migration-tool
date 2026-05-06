#!/usr/bin/env python3
"""
Example usage of GalaxyAPIClient for Automation Hub operations.

This script demonstrates how to use the GalaxyAPIClient to interact with
Automation Hub's Galaxy API for listing namespaces, collections, etc.

Usage:
    # List all namespaces
    python examples/automation_hub_client_example.py list-namespaces

    # Get specific namespace
    python examples/automation_hub_client_example.py get-namespace ansible

    # List collections in namespace
    python examples/automation_hub_client_example.py list-collections ansible
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aap_migration.automation_hub import GalaxyAPIClient, Namespace


async def list_namespaces_example():
    """Example: List all namespaces."""
    url = os.getenv("HUB_URL", "https://localhost:10443")
    token = os.getenv("HUB_TOKEN", "")

    if not token:
        print("Error: HUB_TOKEN environment variable required")
        return

    async with GalaxyAPIClient(url, token, verify_ssl=False) as client:
        print(f"Connecting to: {url}")
        print("Fetching namespaces...\n")

        namespaces = await client.list_namespaces()

        print(f"Found {len(namespaces)} namespaces:\n")
        for ns in namespaces:
            print(f"  • {ns.name}")
            if ns.company:
                print(f"    Company: {ns.company}")
            if ns.description:
                print(f"    Description: {ns.description}")
            print()


async def get_namespace_example(name: str):
    """Example: Get specific namespace."""
    url = os.getenv("HUB_URL", "https://localhost:10443")
    token = os.getenv("HUB_TOKEN", "")

    if not token:
        print("Error: HUB_TOKEN environment variable required")
        return

    async with GalaxyAPIClient(url, token, verify_ssl=False) as client:
        print(f"Fetching namespace: {name}\n")

        try:
            ns = await client.get_namespace(name)

            print(f"Namespace: {ns.name}")
            print(f"  Company: {ns.company or 'N/A'}")
            print(f"  Email: {ns.email or 'N/A'}")
            print(f"  Description: {ns.description or 'N/A'}")
            print(f"  Source ID: {ns.source_id}")

        except Exception as e:
            print(f"Error: {e}")


async def list_collections_example(namespace: str):
    """Example: List collections in namespace."""
    url = os.getenv("HUB_URL", "https://localhost:10443")
    token = os.getenv("HUB_TOKEN", "")

    if not token:
        print("Error: HUB_TOKEN environment variable required")
        return

    async with GalaxyAPIClient(url, token, verify_ssl=False) as client:
        print(f"Fetching collections for namespace: {namespace}\n")

        versions = await client.list_collections(namespace=namespace)

        # Group by collection
        collections = {}
        for v in versions:
            key = f"{v.namespace}.{v.name}"
            if key not in collections:
                collections[key] = []
            collections[key].append(v.version)

        print(f"Found {len(collections)} collections:\n")
        for coll_name, versions in sorted(collections.items()):
            print(f"  • {coll_name}")
            print(f"    Versions: {', '.join(sorted(versions))}")
            print()


async def check_namespace_exists_example(name: str):
    """Example: Check if namespace exists."""
    url = os.getenv("HUB_URL", "https://localhost:10443")
    token = os.getenv("HUB_TOKEN", "")

    if not token:
        print("Error: HUB_TOKEN environment variable required")
        return

    async with GalaxyAPIClient(url, token, verify_ssl=False) as client:
        print(f"Checking if namespace exists: {name}\n")

        exists = await client.namespace_exists(name)

        if exists:
            print(f"✓ Namespace '{name}' exists")
        else:
            print(f"✗ Namespace '{name}' does not exist")


async def create_namespace_example(name: str, company: str):
    """Example: Create a namespace."""
    url = os.getenv("HUB_URL", "https://localhost:10443")
    token = os.getenv("HUB_TOKEN", "")

    if not token:
        print("Error: HUB_TOKEN environment variable required")
        return

    async with GalaxyAPIClient(url, token, verify_ssl=False) as client:
        print(f"Creating namespace: {name}\n")

        ns = Namespace(
            name=name,
            company=company,
            description=f"Example namespace for {company}",
        )

        try:
            created = await client.create_namespace(ns)
            print(f"✓ Namespace created: {created.name}")
            print(f"  ID: {created.target_id}")
            print(f"  Company: {created.company}")
        except Exception as e:
            print(f"✗ Failed to create namespace: {e}")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python examples/automation_hub_client_example.py <command> [args...]")
        print()
        print("Commands:")
        print("  list-namespaces              - List all namespaces")
        print("  get-namespace <name>         - Get specific namespace")
        print("  list-collections <namespace> - List collections in namespace")
        print("  check-namespace <name>       - Check if namespace exists")
        print("  create-namespace <name> <company> - Create new namespace")
        print()
        print("Environment Variables:")
        print("  HUB_URL   - Automation Hub URL (default: https://localhost:10443)")
        print("  HUB_TOKEN - Authentication token (required)")
        return

    command = sys.argv[1]

    if command == "list-namespaces":
        asyncio.run(list_namespaces_example())

    elif command == "get-namespace":
        if len(sys.argv) < 3:
            print("Error: namespace name required")
            return
        asyncio.run(get_namespace_example(sys.argv[2]))

    elif command == "list-collections":
        if len(sys.argv) < 3:
            print("Error: namespace name required")
            return
        asyncio.run(list_collections_example(sys.argv[2]))

    elif command == "check-namespace":
        if len(sys.argv) < 3:
            print("Error: namespace name required")
            return
        asyncio.run(check_namespace_exists_example(sys.argv[2]))

    elif command == "create-namespace":
        if len(sys.argv) < 4:
            print("Error: namespace name and company required")
            return
        asyncio.run(create_namespace_example(sys.argv[2], sys.argv[3]))

    else:
        print(f"Unknown command: {command}")
        print("Run without arguments to see usage")


if __name__ == "__main__":
    main()
