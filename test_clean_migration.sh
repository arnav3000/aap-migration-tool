#!/bin/bash
set -e

echo "================================================================"
echo "CLEAN MIGRATION TEST - Verify Orphaned Mappings Fix"
echo "================================================================"
echo ""

# Backup current database
echo "📦 Backing up current database..."
cp migration_state.db migration_state.db.before_clean_test
echo "   ✅ Backup saved to: migration_state.db.before_clean_test"
echo ""

# Delete database for clean test
echo "🗑️  Deleting database for clean test..."
rm -f migration_state.db
echo "   ✅ Database deleted"
echo ""

# Import organizations fresh
echo "🔄 Importing organizations from scratch..."
source .venv/bin/activate
echo "y" | aap-bridge import -r organizations --input xformed/ 2>&1 | tail -20
echo ""

# Check results
echo "📊 Checking results..."
echo ""

# Count in database
DB_COUNT=$(sqlite3 migration_state.db "SELECT COUNT(*) FROM migration_progress WHERE resource_type = 'organizations';")
echo "   Organizations in migration_progress: $DB_COUNT"

# Count on target
TOKEN=$(grep "TARGET__TOKEN" .env | cut -d'=' -f2 | tr -d '"')
TARGET_COUNT=$(curl -sk -H "Authorization: Bearer $TOKEN" "https://localhost:10443/api/controller/v2/organizations/?page_size=100" | jq -r '.count')
echo "   Organizations on target AAP:         $TARGET_COUNT"

# Count in source file
FILE_COUNT=$(jq 'length' xformed/organizations/organizations_0001.json)
echo "   Organizations in source file:        $FILE_COUNT"
echo ""

# Check for orphaned mappings
echo "🔍 Checking for orphaned ID mappings..."
ORPHANED=$(sqlite3 migration_state.db "
SELECT COUNT(*)
FROM (
    SELECT m.source_id
    FROM id_mappings m
    LEFT JOIN migration_progress p
        ON m.resource_type = p.resource_type
        AND m.source_id = p.source_id
    WHERE m.resource_type = 'organizations'
      AND p.source_id IS NULL
);
")

if [ "$ORPHANED" -eq 0 ]; then
    echo "   ✅ NO orphaned mappings found!"
else
    echo "   ❌ Found $ORPHANED orphaned mappings!"
fi
echo ""

# Final verdict
echo "================================================================"
if [ "$DB_COUNT" -eq "$FILE_COUNT" ] && [ "$ORPHANED" -eq 0 ]; then
    echo "✅ TEST PASSED"
    echo "   - All organizations properly tracked"
    echo "   - No orphaned mappings"
    echo "   - Fix is working correctly"
else
    echo "❌ TEST FAILED"
    echo "   - Database count: $DB_COUNT, Expected: $FILE_COUNT"
    echo "   - Orphaned mappings: $ORPHANED"
fi
echo "================================================================"
