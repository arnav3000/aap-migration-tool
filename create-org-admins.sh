#!/bin/bash
#
# Create Organization Admin Users in AAP 2.4
#
# This script creates an admin user for each organization in source AAP.
# Username: admin_<org_name>
# Password: ansible123
# Role: Organization Admin for their respective organization
#
# Usage:
#   ./create-org-admins.sh
#
# Prerequisites:
#   - SOURCE_AAP_URL and SOURCE_AAP_TOKEN in .env
#   - Or set them as environment variables
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Load .env if it exists
if [[ -f .env ]]; then
    echo -e "${BLUE}→${NC} Loading credentials from .env"
    export $(grep -v '^#' .env | grep -E 'SOURCE_AAP_URL|SOURCE_AAP_TOKEN' | xargs)
fi

# Validate credentials
if [[ -z "$SOURCE_AAP_URL" ]] || [[ -z "$SOURCE_AAP_TOKEN" ]]; then
    echo -e "${RED}❌ Error: SOURCE_AAP_URL and SOURCE_AAP_TOKEN must be set${NC}"
    echo ""
    echo "Either:"
    echo "  1. Set them in .env file"
    echo "  2. Export them as environment variables:"
    echo "     export SOURCE_AAP_URL=https://your-aap.example.com"
    echo "     export SOURCE_AAP_TOKEN=your_token_here"
    exit 1
fi

# Configuration
PASSWORD="ansible123"
# Strip /api/v2 suffix if present in SOURCE_AAP_URL
BASE_URL="${SOURCE_AAP_URL%/api/v2}"
BASE_URL="${BASE_URL%/}"  # Remove trailing slash if any
API_BASE="${BASE_URL}/api/v2"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Create Organization Admin Users${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "Source AAP: ${SOURCE_AAP_URL}"
echo "Password: ${PASSWORD}"
echo ""

# Confirm before proceeding
read -p "Continue? [y/N]: " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""

# Fetch all organizations
echo -e "${YELLOW}→${NC} Fetching organizations from source AAP..."

RESPONSE=$(curl -sk --max-time 30 --connect-timeout 10 \
    -H "Authorization: Bearer ${SOURCE_AAP_TOKEN}" \
    "${API_BASE}/organizations/?page_size=200" 2>&1)

CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
    echo -e "${RED}❌ curl failed with exit code ${CURL_EXIT}${NC}"
    echo "Response: $RESPONSE"
    exit 1
fi

ORGS_JSON=$(echo "$RESPONSE" | jq -r '.results // empty')

if [[ -z "$ORGS_JSON" ]] || [[ "$ORGS_JSON" == "null" ]]; then
    echo -e "${RED}❌ Failed to fetch organizations${NC}"
    echo "Full response:"
    echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
    exit 1
fi

ORG_COUNT=$(echo "$ORGS_JSON" | jq '. | length')
echo -e "${GREEN}✓${NC} Found ${ORG_COUNT} organizations"
echo ""

# Create users for each organization
CREATED=0
SKIPPED=0
FAILED=0

while read -r org; do
    ORG_ID=$(echo "$org" | jq -r '.id')
    ORG_NAME=$(echo "$org" | jq -r '.name')

    # Skip Default organization
    if [[ "$ORG_NAME" == "Default" ]]; then
        echo -e "${BLUE}⏭️  Skipping Default organization${NC}"
        ((SKIPPED++)) || true
        continue
    fi

    # Create sanitized username (replace spaces/special chars with underscore)
    USERNAME="admin_$(echo "$ORG_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/_/g')"

    echo -e "${YELLOW}→${NC} Processing: ${ORG_NAME} (ID: ${ORG_ID})"
    echo "   Username: ${USERNAME}"

    # Check if user already exists
    EXISTING_USER=$(curl -sk -H "Authorization: Bearer ${SOURCE_AAP_TOKEN}" \
        "${API_BASE}/users/?username=${USERNAME}" | jq -r '.results[0].id // empty')

    if [[ -n "$EXISTING_USER" ]]; then
        echo -e "${YELLOW}   ⚠️  User already exists (ID: ${EXISTING_USER})${NC}"
        ((SKIPPED++)) || true

        # Check if user is already org admin
        IS_ADMIN=$(curl -sk -H "Authorization: Bearer ${SOURCE_AAP_TOKEN}" \
            "${API_BASE}/organizations/${ORG_ID}/admins/" | jq -r ".results[] | select(.id == ${EXISTING_USER}) | .id // empty")

        if [[ -z "$IS_ADMIN" ]]; then
            echo -e "${YELLOW}   → Adding user as Organization Admin${NC}"
            RESPONSE=$(curl -sk -X POST \
                -H "Authorization: Bearer ${SOURCE_AAP_TOKEN}" \
                -H "Content-Type: application/json" \
                "${API_BASE}/organizations/${ORG_ID}/admins/" \
                -d "{\"id\": ${EXISTING_USER}}")

            if echo "$RESPONSE" | jq -e '.id' >/dev/null 2>&1; then
                echo -e "${GREEN}   ✓ Added as Organization Admin${NC}"
            else
                echo -e "${RED}   ❌ Failed to add as admin${NC}"
            fi
        else
            echo -e "${GREEN}   ✓ Already Organization Admin${NC}"
        fi
        echo ""
        continue
    fi

    # Create user
    echo "   → Creating user..."
    USER_DATA=$(cat <<EOF
{
    "username": "${USERNAME}",
    "password": "${PASSWORD}",
    "email": "${USERNAME}@example.com",
    "first_name": "Admin",
    "last_name": "${ORG_NAME}",
    "is_superuser": false,
    "is_system_auditor": false
}
EOF
)

    CREATE_RESPONSE=$(curl -sk -X POST \
        -H "Authorization: Bearer ${SOURCE_AAP_TOKEN}" \
        -H "Content-Type: application/json" \
        "${API_BASE}/users/" \
        -d "$USER_DATA")

    USER_ID=$(echo "$CREATE_RESPONSE" | jq -r '.id // empty')

    if [[ -z "$USER_ID" ]]; then
        echo -e "${RED}   ❌ Failed to create user${NC}"
        echo "$CREATE_RESPONSE" | jq -r '.detail // .error // .' | head -3
        ((FAILED++)) || true
        echo ""
        continue
    fi

    echo -e "${GREEN}   ✓ User created (ID: ${USER_ID})${NC}"

    # Make user Organization Admin
    echo "   → Adding as Organization Admin..."
    ROLE_RESPONSE=$(curl -sk -X POST \
        -H "Authorization: Bearer ${SOURCE_AAP_TOKEN}" \
        -H "Content-Type: application/json" \
        "${API_BASE}/organizations/${ORG_ID}/admins/" \
        -d "{\"id\": ${USER_ID}}")

    if echo "$ROLE_RESPONSE" | jq -e '.id' >/dev/null 2>&1; then
        echo -e "${GREEN}   ✓ Added as Organization Admin${NC}"
        ((CREATED++)) || true
    else
        echo -e "${RED}   ❌ Failed to assign Organization Admin role${NC}"
        echo "$ROLE_RESPONSE" | jq -r '.detail // .error // .' | head -3
        ((FAILED++)) || true
    fi

    echo ""
done < <(echo "$ORGS_JSON" | jq -c '.[]')

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✓ Process Complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "Summary:"
echo "  Created: ${CREATED} users"
echo "  Skipped: ${SKIPPED} (already exist)"
echo "  Failed: ${FAILED}"
echo ""
echo "Credentials:"
echo "  Password (all users): ${PASSWORD}"
echo ""
echo "Next steps:"
echo "  1. Create API tokens for each user in AAP UI"
echo "  2. Or use: ./create-org-tokens.sh (if available)"
echo "  3. Add tokens to org.txt file"
echo ""
