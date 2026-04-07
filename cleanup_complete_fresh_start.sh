#!/bin/bash
#
# Complete Fresh Start - Cleanup Everything
# Combines local and target cleanup for full reset
#

set -e

echo "=========================================="
echo "COMPLETE FRESH START CLEANUP"
echo "=========================================="
echo ""
echo "This script will perform a COMPLETE cleanup:"
echo ""
echo "LOCAL CLEANUP:"
echo "  - Remove migration_state.db (database)"
echo "  - Remove exported/ directory"
echo "  - Remove xformed/ directory"
echo "  - Remove migration reports"
echo ""
echo "TARGET AAP 2.6 CLEANUP:"
echo "  - Gateway authenticators (except Local DB)"
echo "  - Gateway authenticator maps"
echo "  - Organizations (except Default)"
echo "  - Projects (except Demo Project)"
echo "  - Inventories (except Demo Inventory)"
echo "  - Hosts (all)"
echo "  - Job Templates (all)"
echo "  - Workflow Templates (all)"
echo "  - Credentials (except Demo Credential)"
echo "  - Custom Credential Types"
echo "  - Teams (all)"
echo "  - Users (except admin/_system)"
echo "  - Schedules (all)"
echo "  - Notification Templates (all)"
echo "  - Labels (all)"
echo "  - Applications (all)"
echo ""
echo "⚠️  WARNING: This is IRREVERSIBLE!"
echo ""
read -p "Type 'CLEANUP' to confirm complete cleanup: " confirm

if [ "$confirm" != "CLEANUP" ]; then
    echo "Cleanup cancelled."
    exit 0
fi

echo ""
echo "=========================================="
echo "Step 1: Local Cleanup"
echo "=========================================="
echo ""

# Run local cleanup
./cleanup_for_fresh_test.sh

echo ""
echo "=========================================="
echo "Step 2: Target Environment Cleanup"
echo "=========================================="
echo ""

# Run target cleanup (will ask for confirmation again)
echo "yes" | ./cleanup_target_environment.sh

echo ""
echo "=========================================="
echo "✅ COMPLETE FRESH START READY"
echo "=========================================="
echo ""
echo "Your environment is now completely clean:"
echo ""
echo "LOCAL:"
echo "  ✓ No migration database"
echo "  ✓ No exported data"
echo "  ✓ No transformed data"
echo "  ✓ No migration reports"
echo ""
echo "TARGET AAP 2.6:"
echo "  ✓ Only Default organization"
echo "  ✓ Only admin user"
echo "  ✓ Only Local DB authenticator"
echo "  ✓ No migrated resources"
echo ""
echo "You can now follow README.md to test complete migration from scratch!"
echo ""
echo "Quick start:"
echo "  1. aap-bridge migrate --help"
echo "  2. aap-bridge migrate full --config config/config.yaml"
echo ""
