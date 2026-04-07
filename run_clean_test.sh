#!/bin/bash
set -e

echo "=================================================================="
echo "CLEAN TEST CYCLE - Orphaned ID Mappings Fix"
echo "=================================================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

source .venv/bin/activate
source .env

TOKEN=$(echo $TARGET__TOKEN | tr -d '"')
TARGET_URL="https://localhost:10443/api/controller/v2"

echo -e "${BLUE}Test 1: Import Organizations${NC}"
echo "=================================================================="
echo ""

# Import organizations
echo "Running: aap-bridge import -r organizations --input xformed/"
echo ""
echo "y" | aap-bridge import -r organizations --input xformed/ 2>&1 | tail -30

echo ""
echo -e "${BLUE}Test 2: Verify Database State${NC}"
echo "=================================================================="

# Check migration_progress
DB_COUNT=$(sqlite3 migration_state.db "SELECT COUNT(*) FROM migration_progress WHERE resource_type = 'organizations';")
echo "Organizations in migration_progress: $DB_COUNT"

# Check id_mappings
MAP_COUNT=$(sqlite3 migration_state.db "SELECT COUNT(*) FROM id_mappings WHERE resource_type = 'organizations';")
echo "Organizations in id_mappings:         $MAP_COUNT"

# Check target
TARGET_COUNT=$(curl -sk -H "Authorization: Bearer $TOKEN" \
    "${TARGET_URL}/organizations/?page_size=1" | jq -r '.count')
echo "Organizations on target AAP:          $TARGET_COUNT"

# Check source file
FILE_COUNT=$(jq 'length' xformed/organizations/organizations_0001.json)
echo "Organizations in source file:         $FILE_COUNT"

echo ""
echo -e "${BLUE}Test 3: Check for Orphaned Mappings${NC}"
echo "=================================================================="

python3 test_orphaned_mappings_fix.py

echo ""
echo -e "${BLUE}Test 4: Detailed Validation${NC}"
echo "=================================================================="

# Run detailed SQL checks
sqlite3 migration_state.db << 'SQL'
.mode column
.headers on

-- Show all organizations
SELECT
    'All Organizations in DB:' as section,
    source_id,
    source_name,
    status,
    target_id
FROM migration_progress
WHERE resource_type = 'organizations'
ORDER BY source_id;

-- Check for mismatches
SELECT
    'Count Validation:' as section,
    'migration_progress' as table_name,
    COUNT(*) as count
FROM migration_progress
WHERE resource_type = 'organizations'
UNION ALL
SELECT
    '',
    'id_mappings',
    COUNT(*)
FROM id_mappings
WHERE resource_type = 'organizations';

-- Check for orphaned mappings
SELECT
    'Orphaned Mappings Check:' as section,
    COUNT(*) as orphaned_count
FROM id_mappings m
LEFT JOIN migration_progress p
    ON m.resource_type = p.resource_type
    AND m.source_id = p.source_id
WHERE m.resource_type = 'organizations'
  AND p.source_id IS NULL;
SQL

echo ""
echo -e "${BLUE}Test 5: Verify Specific Organizations${NC}"
echo "=================================================================="

# Check Default and IT Operations specifically
sqlite3 migration_state.db << 'SQL'
.mode column
.headers on

SELECT
    source_id,
    source_name,
    status,
    target_id,
    CASE
        WHEN source_id = 1 THEN '(Default)'
        WHEN source_id = 6 THEN '(IT Operations)'
        ELSE ''
    END as note
FROM migration_progress
WHERE resource_type = 'organizations'
  AND source_id IN (1, 6)
ORDER BY source_id;
SQL

# Verify on target
echo ""
echo "Verifying Default and IT Operations on target:"
for org_name in "Default" "IT Operations"; do
    result=$(curl -sk -H "Authorization: Bearer $TOKEN" \
        "${TARGET_URL}/organizations/?name=${org_name// /%20}" | \
        jq -r '.results[0] | {id, name} // "Not found"')
    echo "  $org_name: $result"
done

echo ""
echo -e "${BLUE}Final Results${NC}"
echo "=================================================================="

# Calculate success
ORPHANED=$(sqlite3 migration_state.db "
SELECT COUNT(*)
FROM id_mappings m
LEFT JOIN migration_progress p
    ON m.resource_type = p.resource_type
    AND m.source_id = p.source_id
WHERE m.resource_type = 'organizations'
  AND p.source_id IS NULL;
")

ALL_TRACKED=$([ "$DB_COUNT" -eq "$FILE_COUNT" ] && echo "yes" || echo "no")
COUNTS_MATCH=$([ "$DB_COUNT" -eq "$MAP_COUNT" ] && echo "yes" || echo "no")

echo ""
if [ "$ORPHANED" -eq 0 ] && [ "$ALL_TRACKED" == "yes" ] && [ "$COUNTS_MATCH" == "yes" ]; then
    echo -e "${GREEN}✅ ✅ ✅ ALL TESTS PASSED ✅ ✅ ✅${NC}"
    echo ""
    echo "Results:"
    echo "  - All $FILE_COUNT organizations tracked in database"
    echo "  - 0 orphaned ID mappings"
    echo "  - Database state consistent"
    echo "  - Fix is working correctly"
    echo ""
    echo -e "${GREEN}Ready to merge to main!${NC}"
else
    echo -e "${RED}❌ TESTS FAILED${NC}"
    echo ""
    echo "Issues:"
    [ "$ORPHANED" -ne 0 ] && echo "  - Found $ORPHANED orphaned mappings"
    [ "$ALL_TRACKED" != "yes" ] && echo "  - Not all organizations tracked (DB: $DB_COUNT, File: $FILE_COUNT)"
    [ "$COUNTS_MATCH" != "yes" ] && echo "  - Count mismatch (migration_progress: $DB_COUNT, id_mappings: $MAP_COUNT)"
    echo ""
    echo -e "${RED}Do NOT merge until issues are resolved!${NC}"
    exit 1
fi

echo ""
echo "=================================================================="
