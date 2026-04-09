#!/bin/bash
# Diagnostic script to identify database path resolution issue
# Run this inside the container to see where the database gets created

echo "=========================================="
echo "AAP Bridge Database Path Diagnostics"
echo "=========================================="
echo ""

echo "1. Current Working Directory:"
pwd
echo ""

echo "2. Contents of .env file:"
if [ -f /app/aap-bridge/.env ]; then
    echo "   .env file exists at /app/aap-bridge/.env"
    grep MIGRATION_STATE_DB_PATH /app/aap-bridge/.env
else
    echo "   ERROR: .env file NOT FOUND at /app/aap-bridge/.env"
fi
echo ""

echo "3. Python environment check:"
python3 << 'EOF'
import os
import sys

print(f"   Current working directory: {os.getcwd()}")
print(f"   Python executable: {sys.executable}")

# Check if dotenv loads correctly
try:
    from dotenv import load_dotenv
    load_dotenv('/app/aap-bridge/.env')
    db_path = os.getenv('MIGRATION_STATE_DB_PATH', 'NOT SET')
    print(f"   MIGRATION_STATE_DB_PATH from env: {db_path}")

    # Resolve relative path
    if db_path.startswith('sqlite:///./'):
        relative_part = db_path.replace('sqlite:///./', '')
        absolute_path = os.path.abspath(relative_part)
        print(f"   Relative path resolves to: {absolute_path}")
except Exception as e:
    print(f"   ERROR loading dotenv: {e}")
EOF
echo ""

echo "4. Volume mount permissions:"
echo "   /app/aap-bridge/database/ contents:"
ls -la /app/aap-bridge/database/ 2>/dev/null || echo "   Directory does not exist or not accessible"
echo ""
echo "   Write test:"
touch /app/aap-bridge/database/test_write.txt 2>/dev/null && echo "   ✓ Can write to database/" || echo "   ✗ Cannot write to database/"
rm -f /app/aap-bridge/database/test_write.txt 2>/dev/null
echo ""

echo "5. Existing database files:"
find /app/aap-bridge -name "migration_state.db" -o -name "*.db" 2>/dev/null
echo ""

echo "6. Home directory check:"
echo "   HOME variable: $HOME"
if [ -d "$HOME/database" ]; then
    echo "   WARNING: database/ directory exists in HOME ($HOME/database/)"
    ls -la $HOME/database/
fi
echo ""

echo "=========================================="
echo "RECOMMENDATION:"
echo "=========================================="
echo "Before running aap-bridge commands, always run:"
echo "   cd /app/aap-bridge"
echo ""
echo "Then verify:"
echo "   pwd    # Should show: /app/aap-bridge"
echo "   aap-bridge credentials compare"
echo "=========================================="
