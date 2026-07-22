#!/usr/bin/env python3
"""Standalone remediation: assign Gateway Platform Auditor roles.

For already-migrated environments where users were imported before the
auditor role assignment fix. Reads transformed user data + state DB to
find auditors and their target IDs, then creates Gateway assignments.

Usage:
    python tools/remediate_auditor_roles.py \
        --data-dir /path/to/migration/data \
        --target-url https://aap26.example.com/api/controller/v2 \
        --target-token <GATEWAY_CAPABLE_TOKEN>

    Or with .env:
    python tools/remediate_auditor_roles.py --data-dir ./container/iam-test

The script uses the same core as the pipeline fix — assign_auditor_roles()
from aap_migration.migration.auditor_roles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aap_migration.migration.auditor_roles import (
    assign_auditor_roles,
    preflight_gateway_access,
)


def load_env(env_path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def find_auditors_in_xformed(data_dir: Path) -> list[dict]:
    """Scan xformed/users/*.json for users with is_system_auditor=True."""
    users_dir = data_dir / "xformed" / "users"
    if not users_dir.exists():
        print(f"ERROR: {users_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    auditors = []
    for json_file in sorted(users_dir.glob("*.json")):
        with open(json_file) as f:
            users = json.load(f)
        for user in users:
            if user.get("is_system_auditor") is True:
                auditors.append({
                    "username": user.get("username", "unknown"),
                    "source_id": user.get("_source_id", user.get("id")),
                })
    return auditors


def resolve_target_ids(data_dir: Path, auditors: list[dict]) -> list[dict]:
    """Look up target_ids from the state DB."""
    import sqlite3

    db_path = data_dir / "database" / "migration_state.db"
    if not db_path.exists():
        db_path = data_dir / "migration_state.db"
    if not db_path.exists():
        print(f"ERROR: migration_state.db not found in {data_dir}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    resolved = []
    for auditor in auditors:
        row = conn.execute(
            "SELECT target_id FROM id_mappings WHERE resource_type='users' AND source_id=?",
            (auditor["source_id"],),
        ).fetchone()
        if row:
            auditor["target_id"] = row[0]
            resolved.append(auditor)
        else:
            print(f"  WARNING: No target_id for {auditor['username']} (source_id={auditor['source_id']})")
    conn.close()
    return resolved


async def main(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)

    # Load token from args or .env
    target_url = args.target_url
    target_token = args.target_token

    if not target_url or not target_token:
        env_path = data_dir / ".env"
        if not env_path.exists():
            env_path = Path(".env")
        env = load_env(env_path)
        target_url = target_url or env.get("TARGET__URL", "")
        target_token = target_token or env.get("TARGET__TOKEN", "")

    if not target_url or not target_token:
        print("ERROR: --target-url and --target-token required (or set in .env)", file=sys.stderr)
        return 1

    print(f"Data dir: {data_dir}")
    print(f"Target:   {target_url}")
    print()

    # Find auditors in transformed data
    auditors = find_auditors_in_xformed(data_dir)
    if not auditors:
        print("No system auditors found in transformed data. Nothing to do.")
        return 0

    print(f"Found {len(auditors)} system auditor(s):")
    for a in auditors:
        print(f"  - {a['username']} (source_id={a['source_id']})")
    print()

    # Resolve target IDs
    auditor_users = resolve_target_ids(data_dir, auditors)
    if not auditor_users:
        print("ERROR: No auditors could be resolved to target IDs", file=sys.stderr)
        return 1

    print(f"Resolved {len(auditor_users)} target ID(s)")
    print()

    # Create a minimal async client
    from aap_migration.config import AAPInstanceConfig
    from aap_migration.client.aap_target_client import AAPTargetClient

    config = AAPInstanceConfig(url=target_url, token=target_token, verify_ssl=False)
    client = AAPTargetClient(config)

    async with client:
        # Preflight
        try:
            role_def_id = await preflight_gateway_access(client)
        except RuntimeError as e:
            print(f"ERROR: Gateway preflight failed: {e}", file=sys.stderr)
            return 1

        print(f"Platform Auditor role_definition ID: {role_def_id}")
        print()

        # Assign roles
        summary = await assign_auditor_roles(client, auditor_users, role_def_id)

    # Report
    print(f"Results: {summary.verified_count}/{summary.auditor_count} assigned+verified")
    print(f"Max sync latency: {summary.sync_latency_ms_max:.0f}ms")

    if summary.failed:
        print(f"\nFAILED ({len(summary.failed)}):")
        for f in summary.failed:
            print(f"  {f.username} (target_id={f.target_id}): {f.error}")
        return 1

    print("\nAll auditor roles assigned and verified.")
    return 0


def cli():
    parser = argparse.ArgumentParser(
        description="Remediate: assign Gateway Platform Auditor roles for already-migrated users"
    )
    parser.add_argument(
        "--data-dir", required=True,
        help="Migration data directory (contains xformed/, database/)"
    )
    parser.add_argument("--target-url", default="", help="Target AAP URL (or set TARGET__URL in .env)")
    parser.add_argument("--target-token", default="", help="Gateway-capable token (or set TARGET__TOKEN in .env)")
    return parser.parse_args()


if __name__ == "__main__":
    args = cli()
    sys.exit(asyncio.run(main(args)))
