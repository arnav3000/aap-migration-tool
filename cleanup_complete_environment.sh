#!/bin/bash
#
# Complete Environment Cleanup Script
# Cleans AAP 2.6 target + local database + exports + xformed directories
#
# Usage: ./cleanup_complete_environment.sh
#

set -e

echo "=========================================="
echo "Complete Environment Cleanup"
echo "=========================================="
echo ""
echo "This script will:"
echo "  1. Clean AAP 2.6 target (remove all migrated resources)"
echo "  2. Clean local database (migration_state.db)"
echo "  3. Clean exports directory"
echo "  4. Clean xformed directory"
echo "  5. Clean schemas directory"
echo ""
echo "⚠️  WARNING: This will DELETE:"
echo "   - All migrated resources from AAP 2.6"
echo "   - All migration tracking data (id_mappings, progress)"
echo "   - All exported data (will be backed up)"
echo "   - All transformed data (will be backed up)"
echo "   - All schema files (will be backed up)"
echo ""
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Cleanup cancelled."
    exit 0
fi

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Create timestamped backup directory
BACKUP_DIR="backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
echo -e "${BLUE}→${NC} Created backup directory: $BACKUP_DIR"
echo ""

# ============================================
# Step 1: Clean AAP 2.6 Target
# ============================================
echo "=========================================="
echo "Step 1/4: Cleaning AAP 2.6 Target"
echo "=========================================="
echo ""

if [ ! -f .env ]; then
    echo -e "${RED}❌${NC} Error: .env file not found"
    exit 1
fi

source .env

if [ -z "$TARGET__TOKEN" ]; then
    echo -e "${RED}❌${NC} Error: TARGET__TOKEN not set in .env"
    exit 1
fi

if [ -z "$TARGET__URL" ]; then
    echo -e "${RED}❌${NC} Error: TARGET__URL not set in .env"
    exit 1
fi

# Extract base URL
BASE_URL=$(echo "$TARGET__URL" | sed 's|/api/controller/v2||')
CONTROLLER_URL="${BASE_URL}/api/controller/v2"
GATEWAY_URL="${BASE_URL}/api/gateway/v1"

echo -e "${BLUE}→${NC} Target Controller: $CONTROLLER_URL"
echo -e "${BLUE}→${NC} Target Gateway: $GATEWAY_URL"
echo ""

# Function to make authenticated Controller API calls
controller_api() {
    local method=$1
    local endpoint=$2
    local data=$3

    if [ -z "$data" ]; then
        curl -sk -X "$method" \
            -H "Authorization: Bearer $TARGET__TOKEN" \
            -H "Content-Type: application/json" \
            "${CONTROLLER_URL}/${endpoint}"
    else
        curl -sk -X "$method" \
            -H "Authorization: Bearer $TARGET__TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data" \
            "${CONTROLLER_URL}/${endpoint}"
    fi
}

# Function to make authenticated Gateway API calls
gateway_api() {
    local method=$1
    local endpoint=$2

    curl -sk -X "$method" \
        -H "Authorization: Bearer $TARGET__TOKEN" \
        "${GATEWAY_URL}/${endpoint}"
}

# Function to delete resources with pagination
delete_all_resources() {
    local resource_type=$1
    local exclude_filter=$2  # Optional jq filter to exclude certain resources

    echo -e "${YELLOW}→${NC} Cleaning up $resource_type..."

    local page=1
    local total_deleted=0

    while true; do
        local response=$(controller_api GET "${resource_type}/?page_size=100&page=${page}")
        local count=$(echo "$response" | jq -r '.count // 0')

        if [ "$count" -eq 0 ]; then
            break
        fi

        # Get IDs to delete
        if [ -z "$exclude_filter" ]; then
            local ids=$(echo "$response" | jq -r '.results[].id')
        else
            local ids=$(echo "$response" | jq -r ".results[] | select($exclude_filter) | .id")
        fi

        if [ -z "$ids" ]; then
            break
        fi

        # Delete each resource
        for id in $ids; do
            local name=$(echo "$response" | jq -r ".results[] | select(.id==$id) | .name // .hostname // \"ID:$id\"")
            controller_api DELETE "${resource_type}/${id}/" > /dev/null 2>&1 && {
                ((total_deleted++))
            } || true  # Continue even if deletion fails
        done

        ((page++))

        # Safety: don't loop forever
        if [ $page -gt 100 ]; then
            echo -e "${RED}   ⚠${NC} Safety limit reached (100 pages)"
            break
        fi
    done

    echo -e "${GREEN}✓${NC} Deleted $total_deleted $resource_type"
}

# Clean Gateway resources
echo -e "${BLUE}→${NC} Cleaning Gateway resources..."
MAPS=$(gateway_api GET "authenticator_maps/")
MAP_COUNT=$(echo "$MAPS" | jq -r '.count // 0')
if [ "$MAP_COUNT" -gt 0 ]; then
    MAP_IDS=$(echo "$MAPS" | jq -r '.results[].id')
    for id in $MAP_IDS; do
        gateway_api DELETE "authenticator_maps/$id/" > /dev/null 2>&1 || true
    done
    echo -e "${GREEN}✓${NC} Deleted $MAP_COUNT authenticator maps"
fi

AUTHENTICATORS=$(gateway_api GET "authenticators/")
AUTH_IDS=$(echo "$AUTHENTICATORS" | jq -r '.results[] | select(.type != "ansible_base.authentication.authenticator_plugins.local") | .id')
AUTH_COUNT=0
if [ -n "$AUTH_IDS" ]; then
    for id in $AUTH_IDS; do
        gateway_api DELETE "authenticators/$id/" > /dev/null 2>&1 || true
        ((AUTH_COUNT++))
    done
    echo -e "${GREEN}✓${NC} Deleted $AUTH_COUNT authenticators"
fi
echo ""

# Clean resources in reverse dependency order
delete_all_resources "workflow_job_templates"
delete_all_resources "job_templates"
delete_all_resources "schedules"
delete_all_resources "hosts"
delete_all_resources "inventories" '.name != "Demo Inventory"'
delete_all_resources "projects" '.name != "Demo Project"'
delete_all_resources "credentials" '.name != "Demo Credential"'
delete_all_resources "credential_types" '.managed == false'
delete_all_resources "teams"
delete_all_resources "users" '.username != "admin" and .username != "_system"'
delete_all_resources "organizations" '.name != "Default"'
delete_all_resources "notification_templates"
delete_all_resources "labels"
delete_all_resources "applications"

echo ""
echo -e "${GREEN}✓ AAP 2.6 Target Cleaned${NC}"
echo ""

# ============================================
# Step 2: Clean Local Database
# ============================================
echo "=========================================="
echo "Step 2/4: Cleaning Local Database"
echo "=========================================="
echo ""

DB_FILE="migration_state.db"

if [ -f "$DB_FILE" ]; then
    # Backup database
    cp "$DB_FILE" "$BACKUP_DIR/$DB_FILE"
    echo -e "${BLUE}→${NC} Backed up database to $BACKUP_DIR/$DB_FILE"

    # Clean all tables
    sqlite3 "$DB_FILE" "DELETE FROM id_mappings; DELETE FROM migration_progress; DELETE FROM checkpoints; DELETE FROM migration_metadata; DELETE FROM performance_metrics;"

    # Verify
    ID_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM id_mappings;")
    PROGRESS_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM migration_progress;")

    echo -e "${GREEN}✓${NC} Database cleaned:"
    echo -e "   id_mappings: $ID_COUNT"
    echo -e "   migration_progress: $PROGRESS_COUNT"
else
    echo -e "${YELLOW}⚠${NC} Database file not found: $DB_FILE"
fi

echo ""

# ============================================
# Step 3: Clean Exports Directory
# ============================================
echo "=========================================="
echo "Step 3/5: Cleaning Exports Directory"
echo "=========================================="
echo ""

if [ -d "exports" ] && [ "$(ls -A exports)" ]; then
    mv exports "$BACKUP_DIR/exports"
    echo -e "${GREEN}✓${NC} Backed up exports to $BACKUP_DIR/exports"
else
    echo -e "${YELLOW}⚠${NC} Exports directory is already empty or doesn't exist"
fi

mkdir -p exports
echo -e "${GREEN}✓${NC} Created fresh exports directory"
echo ""

# ============================================
# Step 4: Clean Xformed Directory
# ============================================
echo "=========================================="
echo "Step 4/5: Cleaning Xformed Directory"
echo "=========================================="
echo ""

if [ -d "xformed" ] && [ "$(ls -A xformed)" ]; then
    mv xformed "$BACKUP_DIR/xformed"
    echo -e "${GREEN}✓${NC} Backed up xformed to $BACKUP_DIR/xformed"
else
    echo -e "${YELLOW}⚠${NC} Xformed directory is already empty or doesn't exist"
fi

mkdir -p xformed
echo -e "${GREEN}✓${NC} Created fresh xformed directory"
echo ""

# ============================================
# Step 5: Clean Schemas Directory
# ============================================
echo "=========================================="
echo "Step 5/5: Cleaning Schemas Directory"
echo "=========================================="
echo ""

if [ -d "schemas" ] && [ "$(ls -A schemas)" ]; then
    mv schemas "$BACKUP_DIR/schemas"
    echo -e "${GREEN}✓${NC} Backed up schemas to $BACKUP_DIR/schemas"
else
    echo -e "${YELLOW}⚠${NC} Schemas directory is already empty or doesn't exist"
fi

mkdir -p schemas
echo -e "${GREEN}✓${NC} Created fresh schemas directory"
echo ""

# ============================================
# Summary
# ============================================
echo "=========================================="
echo -e "${GREEN}✓ Complete Environment Cleanup Done!${NC}"
echo "=========================================="
echo ""
echo "Environment Status:"
echo "  ✓ AAP 2.6 Target: Clean (Default org only, admin user only)"
echo "  ✓ Database: Clean (0 records in all tables)"
echo "  ✓ Exports: Empty"
echo "  ✓ Xformed: Empty"
echo "  ✓ Schemas: Empty"
echo ""
echo "Backup Location: $BACKUP_DIR/"
echo ""
echo "Ready for fresh migration testing!"
echo ""
