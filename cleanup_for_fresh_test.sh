#!/bin/bash
#
# Cleanup Script for Fresh Testing Environment
# Removes all migration artifacts and resets AAP 2.6 target to clean state
#

set -e

echo "=========================================="
echo "AAP Bridge - Fresh Testing Environment Setup"
echo "=========================================="
echo ""

# Load environment variables
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found"
    exit 1
fi

source .env

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "Step 1: Cleaning up local migration artifacts"
echo "----------------------------------------------"

# Remove migration state database
if [ -f migration_state.db ]; then
    echo -e "${YELLOW}→${NC} Removing migration state database..."
    rm -f migration_state.db
    echo -e "${GREEN}✓${NC} Removed migration_state.db"
fi

# Remove exported data
if [ -d exported ]; then
    echo -e "${YELLOW}→${NC} Removing exported data directory..."
    rm -rf exported
    echo -e "${GREEN}✓${NC} Removed exported/"
fi

# Remove transformed data
if [ -d xformed ]; then
    echo -e "${YELLOW}→${NC} Removing transformed data directory..."
    rm -rf xformed
    echo -e "${GREEN}✓${NC} Removed xformed/"
fi

# Remove migration reports
echo -e "${YELLOW}→${NC} Removing migration reports..."
rm -f SETTINGS-REVIEW-REPORT.md
rm -f MIGRATION-REPORT-*.md
rm -f CREDENTIAL-COMPARISON-REPORT.md
echo -e "${GREEN}✓${NC} Removed migration reports"

# Remove test scripts (optional - commented out)
# echo -e "${YELLOW}→${NC} Removing test scripts..."
# rm -f test_ldap_migration.py test_schedules_simple.py test_all_schedules.py
# rm -f fix_duplicate_projects.py sync_inventory_sources.py
# echo -e "${GREEN}✓${NC} Removed test scripts"

# Remove temporary data files
echo -e "${YELLOW}→${NC} Removing temporary data files..."
rm -f source_ldap_settings.json
echo -e "${GREEN}✓${NC} Removed temporary data"

echo ""
echo "Step 2: Cleaning up AAP 2.6 Target Environment"
echo "----------------------------------------------"

if [ -z "$TARGET__TOKEN" ]; then
    echo -e "${RED}❌${NC} Error: TARGET__TOKEN not set in .env"
    exit 1
fi

if [ -z "$TARGET__URL" ]; then
    echo -e "${RED}❌${NC} Error: TARGET__URL not set in .env"
    exit 1
fi

# Extract base URL and construct Gateway URL
BASE_URL=$(echo "$TARGET__URL" | sed 's|/api/controller/v2||')
GATEWAY_URL="${BASE_URL}/api/gateway/v1"

echo -e "${YELLOW}→${NC} Target: $BASE_URL"
echo ""

# Function to make authenticated API calls
api_call() {
    local method=$1
    local endpoint=$2
    local data=$3

    if [ -z "$data" ]; then
        curl -sk -X "$method" \
            -H "Authorization: Bearer $TARGET__TOKEN" \
            "${GATEWAY_URL}/${endpoint}"
    else
        curl -sk -X "$method" \
            -H "Authorization: Bearer $TARGET__TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data" \
            "${GATEWAY_URL}/${endpoint}"
    fi
}

# List existing authenticators
echo -e "${YELLOW}→${NC} Checking existing Gateway authenticators..."
AUTHENTICATORS=$(api_call GET "authenticators/")
AUTH_COUNT=$(echo "$AUTHENTICATORS" | jq -r '.count // 0')

echo "   Found $AUTH_COUNT authenticators"

if [ "$AUTH_COUNT" -gt 0 ]; then
    echo ""
    echo "   Current authenticators:"
    echo "$AUTHENTICATORS" | jq -r '.results[] | "   - ID: \(.id), Name: \(.name), Type: \(.type)"'
    echo ""

    # Get authenticator IDs (exclude the default local database authenticator)
    AUTH_IDS=$(echo "$AUTHENTICATORS" | jq -r '.results[] | select(.type != "ansible_base.authentication.authenticator_plugins.local") | .id')

    if [ -n "$AUTH_IDS" ]; then
        echo -e "${YELLOW}→${NC} Deleting non-local authenticators..."
        for id in $AUTH_IDS; do
            AUTH_NAME=$(echo "$AUTHENTICATORS" | jq -r ".results[] | select(.id==$id) | .name")
            echo "   Deleting: $AUTH_NAME (ID: $id)"
            api_call DELETE "authenticators/$id/" > /dev/null 2>&1
            echo -e "${GREEN}   ✓${NC} Deleted authenticator ID $id"
        done
    else
        echo -e "${GREEN}✓${NC} Only local authenticator present (keeping it)"
    fi
else
    echo -e "${GREEN}✓${NC} No authenticators to clean up"
fi

echo ""
echo -e "${YELLOW}→${NC} Checking authenticator maps..."
MAPS=$(api_call GET "authenticator_maps/")
MAP_COUNT=$(echo "$MAPS" | jq -r '.count // 0')

echo "   Found $MAP_COUNT authenticator maps"

if [ "$MAP_COUNT" -gt 0 ]; then
    echo "$MAPS" | jq -r '.results[] | "   - ID: \(.id), Name: \(.name), Type: \(.map_type)"'
    echo ""
    echo -e "${YELLOW}→${NC} Deleting authenticator maps..."

    MAP_IDS=$(echo "$MAPS" | jq -r '.results[] | .id')
    for id in $MAP_IDS; do
        MAP_NAME=$(echo "$MAPS" | jq -r ".results[] | select(.id==$id) | .name")
        echo "   Deleting: $MAP_NAME (ID: $id)"
        api_call DELETE "authenticator_maps/$id/" > /dev/null 2>&1
        echo -e "${GREEN}   ✓${NC} Deleted map ID $id"
    done
else
    echo -e "${GREEN}✓${NC} No authenticator maps to clean up"
fi

echo ""
echo "=========================================="
echo -e "${GREEN}✓ Cleanup Complete!${NC}"
echo "=========================================="
echo ""
echo "Environment is ready for fresh testing:"
echo "  - Local migration state cleared"
echo "  - Exported/transformed data removed"
echo "  - Gateway authenticators removed (except local DB)"
echo "  - Gateway authenticator maps removed"
echo ""
echo "Next steps:"
echo "  1. aap-bridge export -r settings"
echo "  2. aap-bridge transform -r settings"
echo "  3. aap-bridge import -r settings"
echo "  4. Verify authenticators created"
echo ""
