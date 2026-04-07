#!/bin/bash
#
# Reset Stuck Credentials in Migration Database
# Resets credentials stuck in 'in_progress' or 'pending' back to allow retry
#

set -e

echo "=========================================="
echo "Reset Stuck Credentials"
echo "=========================================="
echo ""

if [ ! -f migration_state.db ]; then
    echo "❌ Error: migration_state.db not found"
    exit 1
fi

# Check current status
echo "Current credential migration status:"
sqlite3 migration_state.db "SELECT status, COUNT(*) as count FROM migration_progress WHERE resource_type = 'credentials' GROUP BY status;"
echo ""

# Get stuck credentials
IN_PROGRESS=$(sqlite3 migration_state.db "SELECT COUNT(*) FROM migration_progress WHERE resource_type = 'credentials' AND status = 'in_progress';")
PENDING=$(sqlite3 migration_state.db "SELECT COUNT(*) FROM migration_progress WHERE resource_type = 'credentials' AND status = 'pending';")

echo "Stuck credentials:"
echo "  - In progress: $IN_PROGRESS"
echo "  - Pending: $PENDING"
echo ""

if [ "$IN_PROGRESS" -eq 0 ] && [ "$PENDING" -eq 0 ]; then
    echo "✅ No stuck credentials found. All credentials completed."
    exit 0
fi

echo "Credentials that will be reset:"
sqlite3 migration_state.db "SELECT source_id, source_name, status FROM migration_progress WHERE resource_type = 'credentials' AND (status = 'in_progress' OR status = 'pending') ORDER BY source_id;"
echo ""

read -p "Reset these credentials to allow retry? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Reset cancelled."
    exit 0
fi

echo ""
echo "Resetting stuck credentials..."

# Delete stuck credentials from migration_progress
# This allows them to be re-attempted on next run
sqlite3 migration_state.db "DELETE FROM migration_progress WHERE resource_type = 'credentials' AND (status = 'in_progress' OR status = 'pending');"

# Also remove any ID mappings for in_progress credentials (they may be incomplete)
sqlite3 migration_state.db "DELETE FROM id_mappings WHERE resource_type = 'credentials' AND source_id IN (
    SELECT source_id FROM migration_progress WHERE resource_type = 'credentials' AND status = 'in_progress'
);" 2>/dev/null || true

echo "✅ Reset complete!"
echo ""
echo "Updated status:"
sqlite3 migration_state.db "SELECT status, COUNT(*) as count FROM migration_progress WHERE resource_type = 'credentials' GROUP BY status;"
echo ""
echo "You can now re-run the migration:"
echo "  aap-bridge migrate -r credentials --skip-prep"
echo ""
