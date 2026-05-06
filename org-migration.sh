#!/bin/bash
#
# Organization-specific migration wrapper
#
# This script reads organization-to-token mappings from org.txt,
# updates the .env file with the appropriate token, and runs aap-bridge.
#
# Usage:
#   ./org-migration.sh --organization org1 [additional aap-bridge args]
#
# Example:
#   ./org-migration.sh --organization org1 migrate -r organizations
#   ./org-migration.sh --organization org2 migration-report
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

# Parse --organization argument
ORGANIZATION=""
AAP_BRIDGE_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --organization|-o)
            ORGANIZATION="$2"
            shift 2
            ;;
        *)
            # Collect remaining arguments for aap-bridge
            AAP_BRIDGE_ARGS+=("$1")
            shift
            ;;
    esac
done

# Validate organization was provided
if [[ -z "$ORGANIZATION" ]]; then
    echo -e "${RED}❌ Error: --organization is required${NC}"
    echo ""
    echo "Usage: $0 --organization <org_name> [aap-bridge args]"
    echo ""
    echo "Example:"
    echo "  $0 --organization org1 migrate -r organizations"
    echo "  $0 -o org2 migration-report"
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
echo ""

# Backup .env if not already backed up
if [[ ! -f "${ENV_FILE}.org-backup" ]]; then
    echo -e "${YELLOW}→${NC} Creating backup: ${ENV_FILE}.org-backup"
    cp "$ENV_FILE" "${ENV_FILE}.org-backup"
fi

# Update TARGET_AAP_TOKEN in .env
echo -e "${YELLOW}→${NC} Updating TARGET_AAP_TOKEN in $ENV_FILE"

# Use sed to replace TARGET_AAP_TOKEN value
if grep -q "^TARGET_AAP_TOKEN=" "$ENV_FILE"; then
    # TARGET_AAP_TOKEN exists - replace it
    sed -i.tmp "s|^TARGET_AAP_TOKEN=.*|TARGET_AAP_TOKEN=${TOKEN}|" "$ENV_FILE"
    rm -f "${ENV_FILE}.tmp"
    echo -e "${GREEN}✓${NC} Updated existing TARGET_AAP_TOKEN"
else
    # TARGET_AAP_TOKEN doesn't exist - append it
    echo "TARGET_AAP_TOKEN=${TOKEN}" >> "$ENV_FILE"
    echo -e "${GREEN}✓${NC} Added TARGET_AAP_TOKEN to $ENV_FILE"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Running aap-bridge${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Run aap-bridge with remaining arguments
if [[ ${#AAP_BRIDGE_ARGS[@]} -eq 0 ]]; then
    echo -e "${YELLOW}⚠️  No aap-bridge arguments provided${NC}"
    echo ""
    echo "Examples:"
    echo "  $0 --organization $ORGANIZATION migrate -r organizations"
    echo "  $0 --organization $ORGANIZATION migration-report"
    echo ""
    echo "Run 'aap-bridge --help' for available commands"
else
    echo -e "${GREEN}→${NC} Command: aap-bridge ${AAP_BRIDGE_ARGS[*]}"
    echo ""

    # Check if we're in a container or host
    if command -v aap-bridge &> /dev/null; then
        # aap-bridge is available - run directly
        aap-bridge "${AAP_BRIDGE_ARGS[@]}"
    else
        echo -e "${YELLOW}⚠️  aap-bridge not found in PATH${NC}"
        echo ""
        echo "If using containerized setup:"
        echo "  1. Start container: podman run -v .env:/app/aap-bridge/.env ..."
        echo "  2. Enter container: podman exec -it <container> /bin/bash"
        echo "  3. Run: aap-bridge ${AAP_BRIDGE_ARGS[*]}"
        echo ""
        echo "Or activate virtual environment:"
        echo "  source .venv/bin/activate"
        echo "  aap-bridge ${AAP_BRIDGE_ARGS[*]}"
        exit 1
    fi
fi
