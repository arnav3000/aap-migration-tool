#!/bin/bash
set -e

echo "=================================================================="
echo "PREPARE FRESH TEST ENVIRONMENT"
echo "Testing: Orphaned ID Mappings Fix"
echo "=================================================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Load environment
source .venv/bin/activate
source .env

TOKEN=$(echo $TARGET__TOKEN | tr -d '"')
TARGET_URL="https://localhost:10443/api/controller/v2"

echo -e "${BLUE}Step 1: Backup Current State${NC}"
echo "=================================================================="

# Backup database if it exists
if [ -f migration_state.db ]; then
    BACKUP_NAME="migration_state.db.backup_$(date +%Y%m%d_%H%M%S)"
    cp migration_state.db "$BACKUP_NAME"
    echo -e "${GREEN}✅ Database backed up to: $BACKUP_NAME${NC}"
else
    echo -e "${YELLOW}⚠️  No database to backup${NC}"
fi
echo ""

echo -e "${BLUE}Step 2: Clean Target AAP Environment${NC}"
echo "=================================================================="

# Function to delete resources
delete_resources() {
    local resource_type=$1
    local resource_name=$2

    echo -e "${YELLOW}Cleaning ${resource_name}...${NC}"

    # Get all resources
    local count=$(curl -sk -H "Authorization: Bearer $TOKEN" \
        "${TARGET_URL}/${resource_type}/?page_size=1" | jq -r '.count')

    echo "  Found: $count ${resource_name}"

    if [ "$count" -gt 0 ]; then
        # Get all IDs (excluding Default org and system resources)
        local ids=$(curl -sk -H "Authorization: Bearer $TOKEN" \
            "${TARGET_URL}/${resource_type}/?page_size=200" | \
            jq -r '.results[] | select(.name != "Default" and .id != 1) | .id' 2>/dev/null)

        local deleted=0
        for id in $ids; do
            # Delete each resource
            result=$(curl -sk -X DELETE \
                -H "Authorization: Bearer $TOKEN" \
                "${TARGET_URL}/${resource_type}/${id}/" \
                -w "%{http_code}" -o /dev/null)

            if [ "$result" -eq 204 ] || [ "$result" -eq 202 ]; then
                ((deleted++))
            fi
        done
        echo -e "${GREEN}  ✅ Deleted: $deleted ${resource_name}${NC}"
    else
        echo -e "${GREEN}  ✅ Already clean${NC}"
    fi
}

# Clean in reverse dependency order
echo ""
echo "🗑️  Deleting migrated resources from target..."
echo ""

# Job execution artifacts (optional - if you want to clean these too)
# delete_resources "jobs" "Jobs"

# RBAC (optional)
# delete_resources "role_user_assignments" "User Role Assignments"
# delete_resources "role_team_assignments" "Team Role Assignments"

# Schedules
delete_resources "schedules" "Schedules"

# Workflows and Templates
delete_resources "workflow_job_templates" "Workflow Job Templates"
delete_resources "job_templates" "Job Templates"

# Inventory data
delete_resources "hosts" "Hosts"
delete_resources "groups" "Inventory Groups"
delete_resources "inventory_sources" "Inventory Sources"
delete_resources "inventories" "Inventories"

# Projects and Execution Environments
delete_resources "projects" "Projects"
delete_resources "execution_environments" "Execution Environments"

# Credentials and Types
delete_resources "credentials" "Credentials"
# Note: Don't delete managed credential types, only custom ones
CUSTOM_CRED_TYPES=$(curl -sk -H "Authorization: Bearer $TOKEN" \
    "${TARGET_URL}/credential_types/?page_size=200" | \
    jq -r '.results[] | select(.managed == false) | .id')
for id in $CUSTOM_CRED_TYPES; do
    curl -sk -X DELETE -H "Authorization: Bearer $TOKEN" \
        "${TARGET_URL}/credential_types/${id}/" -o /dev/null
done
echo -e "${GREEN}  ✅ Deleted custom credential types${NC}"

# Teams and Users
delete_resources "teams" "Teams"
# Note: Don't delete admin user (id=1)
USER_IDS=$(curl -sk -H "Authorization: Bearer $TOKEN" \
    "${TARGET_URL}/users/?page_size=200" | \
    jq -r '.results[] | select(.id != 1 and .username != "admin") | .id')
for id in $USER_IDS; do
    curl -sk -X DELETE -H "Authorization: Bearer $TOKEN" \
        "${TARGET_URL}/users/${id}/" -o /dev/null 2>&1
done
echo -e "${GREEN}  ✅ Deleted users (kept admin)${NC}"

# Organizations (keep Default)
ORG_IDS=$(curl -sk -H "Authorization: Bearer $TOKEN" \
    "${TARGET_URL}/organizations/?page_size=200" | \
    jq -r '.results[] | select(.id != 1 and .name != "Default") | .id')
deleted_orgs=0
for id in $ORG_IDS; do
    result=$(curl -sk -X DELETE -H "Authorization: Bearer $TOKEN" \
        "${TARGET_URL}/organizations/${id}/" -w "%{http_code}" -o /dev/null)
    if [ "$result" -eq 204 ] || [ "$result" -eq 202 ]; then
        ((deleted_orgs++))
    fi
done
echo -e "${GREEN}  ✅ Deleted $deleted_orgs organizations (kept Default)${NC}"

# Applications (OAuth)
delete_resources "applications" "Applications"

echo ""
echo -e "${BLUE}Step 3: Clean Local Database${NC}"
echo "=================================================================="

# Delete database
if [ -f migration_state.db ]; then
    rm -f migration_state.db
    echo -e "${GREEN}✅ Database deleted${NC}"
else
    echo -e "${YELLOW}⚠️  No database to delete${NC}"
fi

# Clean temporary database backups
rm -f migration_state.db.before_clean_test
echo -e "${GREEN}✅ Temporary backups cleaned${NC}"
echo ""

echo -e "${BLUE}Step 4: Verify Clean State${NC}"
echo "=================================================================="

# Count remaining resources on target
echo ""
echo "Target AAP Resource Counts:"
echo "----------------------------"

# Function to count resources
count_resources() {
    local resource_type=$1
    local resource_name=$2

    local count=$(curl -sk -H "Authorization: Bearer $TOKEN" \
        "${TARGET_URL}/${resource_type}/?page_size=1" | jq -r '.count')

    printf "  %-30s %s\n" "${resource_name}:" "$count"
}

count_resources "organizations" "Organizations"
count_resources "users" "Users"
count_resources "teams" "Teams"
count_resources "credential_types" "Credential Types (all)"
count_resources "credentials" "Credentials"
count_resources "projects" "Projects"
count_resources "execution_environments" "Execution Environments"
count_resources "inventories" "Inventories"
count_resources "hosts" "Hosts"
count_resources "job_templates" "Job Templates"
count_resources "workflow_job_templates" "Workflow Job Templates"

echo ""
echo -e "${BLUE}Step 5: Verify Export/Transform Data${NC}"
echo "=================================================================="

# Check if exports and xformed directories exist and have data
if [ -d "exports" ] && [ -d "exports/organizations" ]; then
    EXPORT_ORG_COUNT=$(jq 'length' exports/organizations/organizations_0001.json 2>/dev/null || echo "0")
    echo -e "${GREEN}✅ Exports directory exists${NC}"
    echo "   Organizations in export: $EXPORT_ORG_COUNT"
else
    echo -e "${RED}❌ No exports directory - run export first${NC}"
fi

if [ -d "xformed" ] && [ -d "xformed/organizations" ]; then
    XFORM_ORG_COUNT=$(jq 'length' xformed/organizations/organizations_0001.json 2>/dev/null || echo "0")
    echo -e "${GREEN}✅ Transformed directory exists${NC}"
    echo "   Organizations in xformed: $XFORM_ORG_COUNT"

    # Check metadata
    METADATA_ORG_COUNT=$(jq -r '.resource_types.organizations.count // 0' xformed/metadata.json 2>/dev/null || echo "0")
    if [ "$METADATA_ORG_COUNT" -eq "$XFORM_ORG_COUNT" ]; then
        echo -e "${GREEN}✅ Metadata is correct${NC}"
    else
        echo -e "${YELLOW}⚠️  Metadata mismatch: file=$XFORM_ORG_COUNT, metadata=$METADATA_ORG_COUNT${NC}"
        echo -e "${YELLOW}   Fixing metadata...${NC}"

        # Fix metadata
        python3 << 'PYEOF'
import json
from pathlib import Path

metadata_path = Path('xformed/metadata.json')
if metadata_path.exists():
    with open(metadata_path) as f:
        metadata = json.load(f)

    # Count orgs in file
    with open('xformed/organizations/organizations_0001.json') as f:
        orgs = json.load(f)
        org_count = len(orgs)

    # Update metadata
    if 'resource_types' not in metadata or not isinstance(metadata['resource_types'], dict):
        metadata['resource_types'] = {}

    metadata['resource_types']['organizations'] = {
        'count': org_count,
        'exported': org_count,
        'transformed': org_count
    }

    with open(metadata_path, 'w') as f:
        json.dump(metadata, f, indent=2)

    print(f"   Fixed metadata: organizations count = {org_count}")
PYEOF
        echo -e "${GREEN}✅ Metadata fixed${NC}"
    fi
else
    echo -e "${RED}❌ No transformed directory - run transform first${NC}"
fi

echo ""
echo -e "${BLUE}Step 6: Summary${NC}"
echo "=================================================================="
echo ""
echo -e "${GREEN}Environment is ready for testing!${NC}"
echo ""
echo "Next steps:"
echo "1. Run migration test:"
echo "   ${YELLOW}./run_clean_test.sh${NC}"
echo ""
echo "2. Or manually test:"
echo "   ${YELLOW}source .venv/bin/activate${NC}"
echo "   ${YELLOW}aap-bridge import -r organizations --input xformed/${NC}"
echo ""
echo "3. Verify results:"
echo "   ${YELLOW}python3 test_orphaned_mappings_fix.py${NC}"
echo ""
echo "=================================================================="
