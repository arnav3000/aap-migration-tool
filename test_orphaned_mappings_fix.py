#!/usr/bin/env python3
"""
Test script to verify orphaned ID mappings fix.

This script tests that all save_id_mapping() calls are now properly
paired with mark_completed() calls.
"""

import sys
import sqlite3
from pathlib import Path

def test_orphaned_mappings():
    """Check for orphaned ID mappings in the database."""

    db_path = Path("migration_state.db")
    if not db_path.exists():
        print("❌ Database not found: migration_state.db")
        return False

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Query to find orphaned mappings
    query = """
    SELECT
        m.resource_type,
        COUNT(DISTINCT m.source_id) as id_mappings,
        COUNT(DISTINCT p.source_id) as migration_progress,
        COUNT(DISTINCT m.source_id) - COUNT(DISTINCT p.source_id) as orphaned
    FROM id_mappings m
    LEFT JOIN migration_progress p
        ON m.resource_type = p.resource_type
        AND m.source_id = p.source_id
    GROUP BY m.resource_type
    HAVING orphaned != 0
    ORDER BY m.resource_type;
    """

    cursor.execute(query)
    results = cursor.fetchall()

    print("\n" + "="*70)
    print("ORPHANED ID MAPPINGS TEST")
    print("="*70)

    if not results:
        print("\n✅ SUCCESS: No orphaned ID mappings found!")
        print("\nAll ID mappings have corresponding migration_progress entries.")
        conn.close()
        return True

    print(f"\n❌ FAILURE: Found orphaned mappings in {len(results)} resource type(s):\n")
    print(f"{'Resource Type':<20} {'ID Mappings':>12} {'Migration Progress':>18} {'Orphaned':>10}")
    print("-" * 70)

    for row in results:
        resource_type, id_maps, migration_prog, orphaned = row
        print(f"{resource_type:<20} {id_maps:>12} {migration_prog:>18} {orphaned:>10}")

    # Show details of orphaned mappings
    print("\n" + "-"*70)
    print("ORPHANED MAPPING DETAILS:")
    print("-"*70)

    for row in results:
        resource_type = row[0]

        detail_query = """
        SELECT m.source_id, m.source_name, m.target_id
        FROM id_mappings m
        LEFT JOIN migration_progress p
            ON m.resource_type = p.resource_type
            AND m.source_id = p.source_id
        WHERE m.resource_type = ?
          AND p.source_id IS NULL
        ORDER BY m.source_id;
        """

        cursor.execute(detail_query, (resource_type,))
        orphans = cursor.fetchall()

        print(f"\n{resource_type}:")
        for source_id, source_name, target_id in orphans:
            print(f"  - Source {source_id:>3} ({source_name:<30}) → Target {target_id}")

    conn.close()
    return False


if __name__ == "__main__":
    success = test_orphaned_mappings()
    sys.exit(0 if success else 1)
