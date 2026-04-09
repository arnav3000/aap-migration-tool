# Database Path Issue Troubleshooting

## Problem
Database file `migration_state.db` gets created in wrong location (home directory instead of `/app/aap-bridge/database/`).

## Root Cause
The `.env` file uses a **relative path**: `sqlite:///./database/migration_state.db`

This path is resolved relative to the **current working directory** when you run `aap-bridge` commands, NOT relative to the container's WORKDIR.

## Diagnosis

### Step 1: Copy diagnostic script into container
```bash
# On host machine
cd /path/to/aap-bridge-fork/container

# Run container with volume mounts
podman run \
  -v $(pwd)/diagnose_db_path.sh:/tmp/diagnose.sh:z \
  -v $(pwd)/logs:/app/aap-bridge/logs:z \
  -v $(pwd)/exports:/app/aap-bridge/exports:z \
  -v $(pwd)/xformed:/app/aap-bridge/xformed:z \
  -v $(pwd)/database:/app/aap-bridge/database:z \
  -v $(pwd)/.env:/app/aap-bridge/.env:z \
  -it localhost/aap-bridge:0.2.5 /bin/bash
```

### Step 2: Run diagnostics inside container
```bash
# Inside container
bash /tmp/diagnose.sh
```

This will show:
- Current working directory when shell starts
- Whether .env file is loaded correctly
- Where the relative path resolves to
- Volume mount permissions

## Solution

### Quick Fix (Always works)
Before running ANY aap-bridge command, run:

```bash
cd /app/aap-bridge
pwd   # Verify you're in /app/aap-bridge
```

Then run your command:
```bash
aap-bridge credentials compare
```

### Permanent Fix Option 1: Update .env to use absolute path
Change in `.env` file:
```bash
# Before (relative path - depends on pwd)
MIGRATION_STATE_DB_PATH=sqlite:///./database/migration_state.db

# After (absolute path - always works)
MIGRATION_STATE_DB_PATH=sqlite:////app/aap-bridge/database/migration_state.db
```

**Note:** Use 4 slashes for absolute path: `sqlite:///` (3 for URI scheme) + `/app/...` (1 for absolute path)

### Permanent Fix Option 2: Update container to set working directory
Modify the container's default command to always start in correct directory:

```dockerfile
# Add to Containerfile
CMD ["/bin/bash", "-c", "cd /app/aap-bridge && exec /bin/bash"]
```

Then rebuild container.

## Verification

After applying fix, verify database is created in correct location:

```bash
# Inside container
cd /app/aap-bridge
aap-bridge credentials compare

# Check database location
ls -la /app/aap-bridge/database/migration_state.db
# Should exist ✓

# Should NOT exist in home directory
ls -la ~/database/migration_state.db
# Should show "No such file or directory" ✓
```

## Why This Happens

**Scenario A - Works correctly:**
```bash
# User A's workflow
podman run ... -it localhost/aap-bridge:0.2.5 /bin/bash
# Shell starts → pwd = /app/aap-bridge (due to WORKDIR in Containerfile)
aap-bridge credentials compare
# Relative path ./database/ resolves to /app/aap-bridge/database/ ✓
```

**Scenario B - Wrong location:**
```bash
# User B's workflow
podman run ... -it localhost/aap-bridge:0.2.5 /bin/bash
# Shell starts → pwd = /home/appuser (shell defaults to HOME directory)
aap-bridge credentials compare
# Relative path ./database/ resolves to /home/appuser/database/ ✗
```

The difference is **which directory the shell starts in** when entering the container.

## Best Practice

**Always use absolute paths in configuration files** to avoid this type of issue:
- ✓ Good: `sqlite:////app/aap-bridge/database/migration_state.db`
- ✗ Bad: `sqlite:///./database/migration_state.db`
