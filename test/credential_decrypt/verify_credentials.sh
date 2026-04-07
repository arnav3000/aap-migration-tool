#!/bin/bash
#
# Quick credential verification script
#

echo "=========================================="
echo "Credential Update Verification"
echo "=========================================="
echo ""

REPORT="credential-update-final.md"

if [ ! -f "$REPORT" ]; then
    echo "❌ Report file not found: $REPORT"
    echo "   Run update script first!"
    exit 1
fi

echo "📊 Checking update report..."
echo ""

# Extract summary stats
TOTAL=$(grep "Total Credentials:" "$REPORT" | awk '{print $NF}')
UPDATED=$(grep "Updated:" "$REPORT" | grep -v "Would Update" | awk '{print $NF}')
FAILED=$(grep "Failed:" "$REPORT" | awk '{print $NF}')
NO_MAPPING=$(grep "No Mapping:" "$REPORT" | awk '{print $NF}')
NO_SECRETS=$(grep "No Secrets:" "$REPORT" | awk '{print $NF}')

echo "Summary:"
echo "  Total Credentials: $TOTAL"
echo "  Updated: $UPDATED"
echo "  Failed: $FAILED"
echo "  No Mapping: $NO_MAPPING"
echo "  No Secrets: $NO_SECRETS"
echo ""

# Validation checks
PASS=0
FAIL=0

if [ "$FAILED" = "0" ]; then
    echo "✅ No failed updates"
    ((PASS++))
else
    echo "❌ $FAILED credentials failed to update"
    ((FAIL++))
fi

EXPECTED_UPDATED=47  # Adjust based on your environment
if [ "$UPDATED" -ge "$EXPECTED_UPDATED" ]; then
    echo "✅ Expected number of credentials updated ($UPDATED >= $EXPECTED_UPDATED)"
    ((PASS++))
else
    echo "⚠️  Fewer credentials updated than expected ($UPDATED < $EXPECTED_UPDATED)"
    ((FAIL++))
fi

if [ "$NO_MAPPING" -le "1" ]; then
    echo "✅ Minimal unmapped credentials ($NO_MAPPING)"
    ((PASS++))
else
    echo "⚠️  Multiple unmapped credentials ($NO_MAPPING)"
    echo "   Check if migration completed successfully"
    ((FAIL++))
fi

echo ""
echo "=========================================="
echo "Validation Score: $PASS passed, $FAIL warnings"
echo "=========================================="
echo ""

if [ $FAIL -eq 0 ]; then
    echo "✅ All checks passed!"
    echo ""
    echo "Next steps:"
    echo "1. Test credentials in AAP 2.6 UI"
    echo "2. Run a job template with migrated credentials"
    echo "3. Securely delete decrypted files:"
    echo "   shred -u scripts/credentials_decrypted.json*"
    exit 0
else
    echo "⚠️  Some checks failed - review the report"
    echo ""
    echo "Review full report:"
    echo "  cat $REPORT"
    exit 1
fi
