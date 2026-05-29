#!/bin/bash
#
# Organization-specific migration wrapper
#
# This script reads organization-to-token mappings from org.txt,
# updates the .env file with the appropriate token, and starts the container.
#
# Usage:
#   ./org-migration.sh --organization <org_name> [--env-file <path>]
#
# Example:
#   ./org-migration.sh --organization Cloud_Services --env-file container/cto-test/.env
#   # Inside container, run your commands manually
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
ORG_TOKEN_FILE="org.txt"
ENV_FILE=".env"

# Parse arguments
ORGANIZATION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --organization|-o)
            ORGANIZATION="$2"
            shift 2
            ;;
        --env-file|-e)
            ENV_FILE="$2"
            shift 2
            ;;
        --org-file)
            ORG_TOKEN_FILE="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}❌ Unknown argument: $1${NC}"
            echo "Usage: $0 --organization <org_name> [--env-file <path>] [--org-file <path>]"
            exit 1
            ;;
    esac
done

# Validate organization was provided
if [[ -z "$ORGANIZATION" ]]; then
    echo -e "${RED}❌ Error: --organization is required${NC}"
    echo ""
    echo "Usage: $0 --organization <org_name> [--env-file <path>]"
    echo ""
    echo "Example:"
    echo "  $0 --organization Cloud_Services"
    echo "  $0 -o Engineering --env-file container/cto-test/.env"
    exit 1
fi

# Check if org.txt exists
if [[ ! -f "$ORG_TOKEN_FILE" ]]; then
    echo -e "${RED}❌ Error: $ORG_TOKEN_FILE not found${NC}"
    echo ""
    echo "Create $ORG_TOKEN_FILE with format:"
    echo "  org1=token123456"
    echo "  org2=token789012"
    exit 1
fi

# Check if .env exists
if [[ ! -f "$ENV_FILE" ]]; then
    echo -e "${RED}❌ Error: $ENV_FILE not found${NC}"
    echo "Make sure you're running this from the project root directory."
    exit 1
fi

# Read token from org.txt
TOKEN=$(grep "^${ORGANIZATION}=" "$ORG_TOKEN_FILE" | cut -d'=' -f2-)

if [[ -z "$TOKEN" ]]; then
    echo -e "${RED}❌ Error: Organization '$ORGANIZATION' not found in $ORG_TOKEN_FILE${NC}"
    echo ""
    echo "Available organizations:"
    grep -E '^[^#].*=' "$ORG_TOKEN_FILE" | cut -d'=' -f1 | sed 's/^/  - /'
    exit 1
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Organization Migration Wrapper${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}✓${NC} Organization: ${ORGANIZATION}"
echo -e "${GREEN}✓${NC} Token loaded: ${TOKEN:0:8}... (${#TOKEN} chars)"
echo -e "${GREEN}✓${NC} Token file: ${ORG_TOKEN_FILE}"
echo -e "${GREEN}✓${NC} ENV file: ${ENV_FILE}"
echo ""

# Backup .env if not already backed up
if [[ ! -f "${ENV_FILE}.org-backup" ]]; then
    echo -e "${YELLOW}→${NC} Creating backup: ${ENV_FILE}.org-backup"
    cp "$ENV_FILE" "${ENV_FILE}.org-backup"
fi

# Update TARGET_AAP_TOKEN in .env
echo -e "${YELLOW}→${NC} Updating TARGET_AAP_TOKEN in $ENV_FILE"

# Replace TARGET_AAP_TOKEN value using grep/echo to avoid sed injection
if grep -q "^TARGET_AAP_TOKEN=" "$ENV_FILE"; then
    # TARGET_AAP_TOKEN exists - remove old line and append new value
    grep -v "^TARGET_AAP_TOKEN=" "$ENV_FILE" > "${ENV_FILE}.tmp"
    echo "TARGET_AAP_TOKEN=${TOKEN}" >> "${ENV_FILE}.tmp"
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
    echo -e "${GREEN}✓${NC} Updated existing TARGET_AAP_TOKEN"
else
    # TARGET_AAP_TOKEN doesn't exist - append it
    echo "TARGET_AAP_TOKEN=${TOKEN}" >> "$ENV_FILE"
    echo -e "${GREEN}✓${NC} Added TARGET_AAP_TOKEN to $ENV_FILE"
fi

echo ""

# Filter exports by organization if exports directory exists
EXPORTS_DIR="exports"
ORG_SANITIZED=$(echo "$ORGANIZATION" | sed 's/[^a-zA-Z0-9]/_/g')
FILTERED_EXPORTS_DIR="exports-${ORG_SANITIZED}"

if [[ -d "$EXPORTS_DIR" ]]; then
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}Filtering Exports${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    echo -e "${YELLOW}→${NC} Full exports found: ${EXPORTS_DIR}"
    echo -e "${YELLOW}→${NC} Filtering for organization: ${ORGANIZATION}"
    echo ""

    # Get script directory to find filter script
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ ! -f "${SCRIPT_DIR}/filter-org-exports.sh" ]]; then
        echo -e "${RED}❌ Error: filter-org-exports.sh not found in ${SCRIPT_DIR}${NC}"
        exit 1
    fi

    # Run filter script
    "${SCRIPT_DIR}/filter-org-exports.sh" --organization "$ORGANIZATION" --exports-dir "$EXPORTS_DIR"

    echo -e "${GREEN}✓${NC} Using filtered exports: ${FILTERED_EXPORTS_DIR}"
    EXPORTS_TO_MOUNT="${FILTERED_EXPORTS_DIR}"
else
    echo -e "${YELLOW}ℹ${NC} No exports directory found - will export inside container"
    EXPORTS_TO_MOUNT="${EXPORTS_DIR}"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Starting Container${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Container image name
CONTAINER_NAME="${CONTAINER_NAME:-localhost/cto-final-test}"

# Check if image exists locally
if ! podman image exists "$CONTAINER_NAME"; then
    echo -e "${RED}❌ Container image not found: ${CONTAINER_NAME}${NC}"
    echo ""
    echo "Available local images:"
    podman images | grep -E "(magnus|aap-bridge)" || echo "  (none found)"
    echo ""
    echo "Build the image first or set CONTAINER_NAME environment variable:"
    echo "  export CONTAINER_NAME=<your_image_name>"
    exit 1
fi

echo -e "${GREEN}→${NC} Container image: ${CONTAINER_NAME}"
echo -e "${YELLOW}→${NC} Starting container with volume mounts..."
echo ""

# Get absolute path to ENV_FILE
ENV_FILE_ABS=$(realpath "$ENV_FILE")

# Get absolute path to exports directory (create if it doesn't exist)
mkdir -p "$EXPORTS_TO_MOUNT"
EXPORTS_TO_MOUNT_ABS=$(realpath "$EXPORTS_TO_MOUNT")

echo -e "${GREEN}✓${NC} Mounting exports: ${EXPORTS_TO_MOUNT}"
echo ""

# Run podman container with all volume mounts
exec podman run \
    -v $(pwd)/logs:/app/aap-bridge/logs:z \
    -v "$EXPORTS_TO_MOUNT_ABS":/app/aap-bridge/exports:z \
    -v $(pwd)/xformed:/app/aap-bridge/xformed:z \
    -v $(pwd)/database:/app/aap-bridge/database:z \
    -v "$ENV_FILE_ABS":/app/aap-bridge/.env:z \
    -v $(pwd)/credential_decrypt:/app/aap-bridge/credential_decrypt:z \
    -it $CONTAINER_NAME /bin/bash
