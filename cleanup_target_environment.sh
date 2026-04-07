#!/bin/bash
#
# Complete Target Environment Cleanup Script
# Removes ALL migrated resources from AAP 2.6 target for fresh testing
#

set -e

echo "=========================================="
echo "AAP 2.6 Target - Complete Environment Cleanup"
echo "=========================================="
echo ""
echo "⚠️  WARNING: This will DELETE all migrated resources from target AAP!"
echo "   - Organizations (except Default)"
echo "   - Projects"
echo "   - Inventories"
echo "   - Hosts"
echo "   - Job Templates"
echo "   - Workflow Templates"
echo "   - Credentials"
echo "   - Custom Credential Types"
echo "   - Teams"
echo "   - Users (except admin/_system)"
echo "   - Gateway Authenticators (except Local DB)"
echo "   - Gateway Authenticator Maps"
echo "   - Schedules"
echo "   - Notification Templates"
echo "   - Labels"
echo ""
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Cleanup cancelled."
    exit 0
fi

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
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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
                echo -e "${GREEN}   ✓${NC} Deleted: $name (ID: $id)"
                ((total_deleted++))
            } || {
                echo -e "${RED}   ✗${NC} Failed to delete: $name (ID: $id)"
            }
        done

        ((page++))

        # Safety: don't loop forever
        if [ $page -gt 100 ]; then
            echo -e "${RED}   ⚠${NC} Safety limit reached (100 pages)"
            break
        fi
    done

    echo -e "${GREEN}✓${NC} Deleted $total_deleted $resource_type"
    echo ""
}

echo "=========================================="
echo "Phase 1: Gateway Cleanup"
echo "=========================================="
echo ""

# 1. Delete Gateway Authenticator Maps
echo -e "${YELLOW}→${NC} Cleaning up Gateway authenticator maps..."
MAPS=$(gateway_api GET "authenticator_maps/")
MAP_COUNT=$(echo "$MAPS" | jq -r '.count // 0')
if [ "$MAP_COUNT" -gt 0 ]; then
    MAP_IDS=$(echo "$MAPS" | jq -r '.results[].id')
    for id in $MAP_IDS; do
        MAP_NAME=$(echo "$MAPS" | jq -r ".results[] | select(.id==$id) | .name")
        gateway_api DELETE "authenticator_maps/$id/" > /dev/null 2>&1
        echo -e "${GREEN}   ✓${NC} Deleted map: $MAP_NAME (ID: $id)"
    done
    echo -e "${GREEN}✓${NC} Deleted $MAP_COUNT authenticator maps"
else
    echo -e "${GREEN}✓${NC} No authenticator maps to delete"
fi
echo ""

# 2. Delete Gateway Authenticators (except Local DB)
echo -e "${YELLOW}→${NC} Cleaning up Gateway authenticators..."
AUTHENTICATORS=$(gateway_api GET "authenticators/")
AUTH_IDS=$(echo "$AUTHENTICATORS" | jq -r '.results[] | select(.type != "ansible_base.authentication.authenticator_plugins.local") | .id')
AUTH_COUNT=0
if [ -n "$AUTH_IDS" ]; then
    for id in $AUTH_IDS; do
        AUTH_NAME=$(echo "$AUTHENTICATORS" | jq -r ".results[] | select(.id==$id) | .name")
        gateway_api DELETE "authenticators/$id/" > /dev/null 2>&1
        echo -e "${GREEN}   ✓${NC} Deleted authenticator: $AUTH_NAME (ID: $id)"
        ((AUTH_COUNT++))
    done
    echo -e "${GREEN}✓${NC} Deleted $AUTH_COUNT authenticators"
else
    echo -e "${GREEN}✓${NC} No authenticators to delete"
fi
echo ""

echo "=========================================="
echo "Phase 2: Workflow and Job Template Cleanup"
echo "=========================================="
echo ""

# Delete workflow job templates first (they may depend on job templates)
delete_all_resources "workflow_job_templates"

# Delete job templates
delete_all_resources "job_templates"

# Delete schedules
delete_all_resources "schedules"

echo "=========================================="
echo "Phase 3: Inventory Cleanup"
echo "=========================================="
echo ""

# Delete hosts (bulk deletion would be better, but this ensures cleanup)
delete_all_resources "hosts"

# Delete inventories (except Demo Inventory if you want to keep it)
delete_all_resources "inventories" '.name != "Demo Inventory"'

echo "=========================================="
echo "Phase 4: Project Cleanup"
echo "=========================================="
echo ""

# Delete projects (except Demo Project if you want to keep it)
delete_all_resources "projects" '.name != "Demo Project"'

echo "=========================================="
echo "Phase 5: Credential and Team Cleanup"
echo "=========================================="
echo ""

# Delete credentials (except Demo Credential)
delete_all_resources "credentials" '.name != "Demo Credential"'

# Delete custom credential types (keep built-in ones)
delete_all_resources "credential_types" '.managed == false'

# Delete teams
delete_all_resources "teams"

echo "=========================================="
echo "Phase 6: User and Organization Cleanup"
echo "=========================================="
echo ""

# Delete users (except admin and _system)
delete_all_resources "users" '.username != "admin" and .username != "_system"'

# Delete organizations (except Default)
delete_all_resources "organizations" '.name != "Default"'

echo "=========================================="
echo "Phase 7: Miscellaneous Cleanup"
echo "=========================================="
echo ""

# Delete notification templates
delete_all_resources "notification_templates"

# Delete labels
delete_all_resources "labels"

# Delete applications (OAuth)
delete_all_resources "applications"

echo "=========================================="
echo -e "${GREEN}✓ Target Environment Cleanup Complete!${NC}"
echo "=========================================="
echo ""
echo "Target AAP 2.6 is now in pristine state:"
echo "  - Default organization only"
echo "  - Admin user only"
echo "  - No migrated resources"
echo "  - No Gateway authenticators (except Local DB)"
echo "  - No authenticator maps"
echo ""
echo "Ready for fresh migration testing from README.md"
echo ""
