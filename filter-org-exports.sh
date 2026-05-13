#!/bin/bash
#
# Filter Exported AAP Data by Organization
#
# This script filters a full AAP export to only include resources
# belonging to a specific organization.
#
# Usage:
#   ./filter-org-exports.sh --organization "Cloud Services" [--exports-dir exports]
#
# Prerequisites:
#   - Full export in exports/ directory (or custom path)
#   - jq installed
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Defaults
EXPORTS_DIR="exports"
ORGANIZATION=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --organization|-o)
            ORGANIZATION="$2"
            shift 2
            ;;
        --exports-dir|-e)
            EXPORTS_DIR="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 --organization <org_name> [--exports-dir <path>]"
            echo ""
            echo "Options:"
            echo "  --organization, -o    Organization name to filter (required)"
            echo "  --exports-dir, -e     Source exports directory (default: exports)"
            echo "  --help, -h            Show this help message"
            echo ""
            echo "Example:"
            echo "  $0 --organization \"Cloud Services\""
            exit 0
            ;;
        *)
            echo -e "${RED}❌ Unknown argument: $1${NC}"
            echo "Run '$0 --help' for usage information"
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "$ORGANIZATION" ]]; then
    echo -e "${RED}❌ Error: --organization is required${NC}"
    echo "Run '$0 --help' for usage information"
    exit 1
fi

# Check if exports directory exists
if [[ ! -d "$EXPORTS_DIR" ]]; then
    echo -e "${RED}❌ Error: Exports directory not found: $EXPORTS_DIR${NC}"
    echo ""
    echo "Run a full export first:"
    echo "  aap-bridge export"
    exit 1
fi

# Check if jq is installed
if ! command -v jq &> /dev/null; then
    echo -e "${RED}❌ Error: jq is required but not installed${NC}"
    echo "Install jq: https://stedolan.github.io/jq/download/"
    exit 1
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Filter Exports by Organization${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}✓${NC} Organization: ${ORGANIZATION}"
echo -e "${GREEN}✓${NC} Source exports: ${EXPORTS_DIR}"
echo ""

# Find organization ID from organizations export
ORG_FILE="${EXPORTS_DIR}/organizations/organizations_0001.json"
if [[ ! -f "$ORG_FILE" ]]; then
    echo -e "${RED}❌ Error: Organizations export not found: $ORG_FILE${NC}"
    exit 1
fi

echo -e "${YELLOW}→${NC} Looking up organization ID..."
ORG_ID=$(jq -r ".results[] | select(.name == \"${ORGANIZATION}\") | .id" "$ORG_FILE")

if [[ -z "$ORG_ID" ]] || [[ "$ORG_ID" == "null" ]]; then
    echo -e "${RED}❌ Error: Organization '${ORGANIZATION}' not found in exports${NC}"
    echo ""
    echo "Available organizations:"
    jq -r '.results[] | "  - \(.name) (ID: \(.id))"' "$ORG_FILE"
    exit 1
fi

echo -e "${GREEN}✓${NC} Found organization: ${ORGANIZATION} (ID: ${ORG_ID})"
echo ""

# Create filtered exports directory
ORG_SANITIZED=$(echo "$ORGANIZATION" | sed 's/[^a-zA-Z0-9]/_/g')
FILTERED_DIR="exports-${ORG_SANITIZED}"

if [[ -d "$FILTERED_DIR" ]]; then
    echo -e "${YELLOW}⚠️  Filtered directory exists: ${FILTERED_DIR}${NC}"
    read -p "Overwrite? [y/N]: " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted."
        exit 0
    fi
    rm -rf "$FILTERED_DIR"
fi

echo -e "${YELLOW}→${NC} Creating filtered exports: ${FILTERED_DIR}"
mkdir -p "$FILTERED_DIR"

# Copy directory structure
echo -e "${YELLOW}→${NC} Copying directory structure..."
find "$EXPORTS_DIR" -type d -exec mkdir -p "$FILTERED_DIR/{}" \; 2>/dev/null || true

# Resource types that should be filtered by organization
ORG_FILTERED_TYPES=(
    "projects"
    "inventories"
    "job_templates"
    "workflow_job_templates"
    "credentials"
    "teams"
    "schedules"
    "notification_templates"
    "labels"
)

# Resource types that are org-specific (just copy the one org)
ORG_SINGLETON_TYPES=(
    "organizations"
)

# Global resource types (copy all - shared resources)
GLOBAL_TYPES=(
    "credential_types"
    "execution_environments"
    "instance_groups"
    "settings"
)

FILTERED_COUNT=0
COPIED_COUNT=0
SKIPPED_COUNT=0

echo ""
echo -e "${BLUE}→${NC} Filtering exports by organization..."
echo ""

# Process each resource type directory
for resource_dir in "$EXPORTS_DIR"/*; do
    if [[ ! -d "$resource_dir" ]]; then
        continue
    fi

    resource_type=$(basename "$resource_dir")

    # Process organization singleton (just the one org)
    if [[ " ${ORG_SINGLETON_TYPES[@]} " =~ " ${resource_type} " ]]; then
        echo -e "${YELLOW}→${NC} Processing ${resource_type} (singleton)..."
        for file in "$resource_dir"/*.json; do
            if [[ -f "$file" ]]; then
                filename=$(basename "$file")

                # Filter to only include this organization
                jq "{
                    count: (.results | map(select(.id == ${ORG_ID})) | length),
                    next: null,
                    previous: null,
                    results: (.results | map(select(.id == ${ORG_ID})))
                }" "$file" > "${FILTERED_DIR}/${resource_type}/${filename}"

                count=$(jq '.count' "${FILTERED_DIR}/${resource_type}/${filename}")
                if [[ "$count" -gt 0 ]]; then
                    echo -e "  ${GREEN}✓${NC} Filtered: ${filename} (${count} items)"
                    ((FILTERED_COUNT++)) || true
                else
                    echo -e "  ${YELLOW}⚠${NC} Skipped: ${filename} (org not found)"
                    ((SKIPPED_COUNT++)) || true
                fi
            fi
        done
        continue
    fi

    # Process org-filtered resources
    if [[ " ${ORG_FILTERED_TYPES[@]} " =~ " ${resource_type} " ]]; then
        echo -e "${YELLOW}→${NC} Processing ${resource_type} (org-filtered)..."
        for file in "$resource_dir"/*.json; do
            if [[ -f "$file" ]]; then
                filename=$(basename "$file")

                # Filter by organization field
                jq "{
                    count: (.results | map(select(.organization == ${ORG_ID} or .summary_fields.organization.id == ${ORG_ID})) | length),
                    next: null,
                    previous: null,
                    results: (.results | map(select(.organization == ${ORG_ID} or .summary_fields.organization.id == ${ORG_ID})))
                }" "$file" > "${FILTERED_DIR}/${resource_type}/${filename}"

                count=$(jq '.count' "${FILTERED_DIR}/${resource_type}/${filename}")
                if [[ "$count" -gt 0 ]]; then
                    echo -e "  ${GREEN}✓${NC} Filtered: ${filename} (${count} items)"
                    ((FILTERED_COUNT++)) || true
                else
                    echo -e "  ${BLUE}ℹ${NC} Skipped: ${filename} (0 items for this org)"
                    ((SKIPPED_COUNT++)) || true
                fi
            fi
        done
        continue
    fi

    # Copy global resources (used by all orgs)
    if [[ " ${GLOBAL_TYPES[@]} " =~ " ${resource_type} " ]]; then
        echo -e "${YELLOW}→${NC} Processing ${resource_type} (global - copy all)..."
        cp -r "$resource_dir" "$FILTERED_DIR/"
        file_count=$(find "$resource_dir" -name "*.json" | wc -l)
        echo -e "  ${GREEN}✓${NC} Copied: ${file_count} files"
        ((COPIED_COUNT+=file_count)) || true
        continue
    fi

    # Unknown resource type - copy as-is with warning
    echo -e "${YELLOW}⚠${NC} Unknown resource type: ${resource_type} (copying all)"
    cp -r "$resource_dir" "$FILTERED_DIR/"
    file_count=$(find "$resource_dir" -name "*.json" | wc -l)
    ((COPIED_COUNT+=file_count)) || true
done

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✓ Filtering Complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "Summary:"
echo "  Organization: ${ORGANIZATION} (ID: ${ORG_ID})"
echo "  Filtered files: ${FILTERED_COUNT}"
echo "  Copied files: ${COPIED_COUNT}"
echo "  Skipped files: ${SKIPPED_COUNT}"
echo ""
echo "Output: ${FILTERED_DIR}/"
echo ""
echo "Next steps:"
echo "  aap-bridge migrate --exports-dir ${FILTERED_DIR} --skip-export"
echo ""
