# Multi-Organization Migration Toolkit

Complete automation for migrating multiple organizations with per-organization admin tokens.

---

## Migration Strategies

### Strategy 1: Full Migration (All-at-Once)
**Use Case:** Small deployments, simple migrations, test environments

```bash
# Use system admin token for both source and target
aap-bridge migrate -r organizations,projects,inventories,job_templates
```

✅ **Pros:** Simple, fast for small deployments  
⚠️ **Cons:** Not suitable for large enterprises (100+ orgs)

---

### Strategy 2: Organization-Based Migration (Recommended for Enterprise)
**Use Case:** Large enterprises (100+ orgs), phased migrations, minimal risk

**Benefits:**
- ✅ Export once, reuse for all organizations (efficient)
- ✅ Migrate one organization at a time (safe, testable)
- ✅ Organization admin tokens (proper RBAC isolation)
- ✅ Automatic filtering (only relevant data per org)

**Workflow:**
```bash
# 1. One-time: Full export (system admin)
aap-bridge export

# 2. Per-org: Filter + migrate (org admin)
./org-migration.sh --organization "Engineering"
# Inside container: aap-bridge migrate --skip-export -r projects,inventories
```

---

### Strategy 3: Phased Export for Large-Scale Migrations ⭐ **RECOMMENDED for 10,000+ Objects**
**Use Case:** Massive deployments (10,000+ resources), minimize time-to-first-org, pipeline parallelism

**Why This is Better:**
- ✅ **Platform ready in 15 minutes** (vs 2+ hours with full export)
- ✅ **Pipeline parallelism** (import Phase 1 while Phase 2 exports)
- ✅ **Granular resume points** (Phase 1 done, only retry Phase 2 if fails)
- ✅ **Early validation** (test with one org before full export completes)

**Workflow:**
```bash
# PHASE 1: Platform Setup (5-15 minutes)
aap-bridge export -r organizations,credential_types,execution_environments
aap-bridge import -r organizations,credential_types,execution_environments
./create-org-admins.sh
./create-org-tokens.sh --target

# ✅ Platform ready! Can start org migrations

# PHASE 2: Content Export (30-90 minutes, runs in background)
aap-bridge export -r credentials,projects,inventories,job_templates,workflow_job_templates,teams,schedules,users

# PHASE 3: Org-Based Migration (5-10 min per org)
./org-migration.sh --organization "Engineering"
# Inside: aap-bridge migrate --skip-export -r credentials,projects,inventories,job_templates,workflow_job_templates,teams
```

**Performance:**
- **Traditional approach:** 120 min export + 15 min setup = **135 min to first org**
- **Phased approach:** 15 min Phase 1 = **15 min to first org** ⚡ (9X faster!)

---

## Scripts Overview

### 1. `create-org-admins.sh`
Creates an organization admin user for each organization in AAP.

**What it does:**
- Fetches all organizations from AAP
- Creates user `admin_<org_name>` with password `ansible123`
- Assigns Organization Admin role

**Usage:**
```bash
# Source AAP
SOURCE_AAP_URL=https://source.example.com SOURCE_AAP_TOKEN=xxx ./create-org-admins.sh

# Target AAP (if needed)
SOURCE_AAP_URL=https://target.example.com SOURCE_AAP_TOKEN=xxx ./create-org-admins.sh
```

**Output:**
```
Created: 5 users
Skipped: 2 (already exist)
Failed: 0
```

---

### 2. `create-org-tokens.sh`
Generates API tokens for all organization admin users.

**What it does:**
- Authenticates as each `admin_<org_name>` user
- Creates a personal API token (scope: write)
- Writes all tokens to `org.txt`

**Usage:**
```bash
# For source AAP (default)
./create-org-tokens.sh

# For target AAP
./create-org-tokens.sh --target
```

**Output:** `org.txt`
```
# Organization Token Mapping
Engineering=$ENGINEERING_TOKEN
Marketing=$MARKETING_TOKEN
Sales=$SALES_TOKEN
```

---

### 3. `filter-org-exports.sh`
Filters a full AAP export to only include resources for a specific organization.

**What it does:**
- Reads full export from `exports/` directory
- Filters all resources by organization ID
- Creates filtered export: `exports-<org_name>/`

**Resource Filtering Rules:**
- **Org-filtered:** projects, inventories, job_templates, workflows, credentials, teams
- **Org-singleton:** organizations (only the specified org)
- **Global-shared:** credential_types, execution_environments (copy all)

**Usage:**
```bash
# After full export
./filter-org-exports.sh --organization "Engineering"

# Custom exports directory
./filter-org-exports.sh --organization "Sales" --exports-dir my-exports
```

**Output:**
```
✓ Found organization: Engineering (ID: 5)
✓ Filtered: 15 projects
✓ Filtered: 8 inventories
✓ Copied: 12 credential types (global)
Output: exports-Engineering/
```

---

### 4. `org-migration.sh`
Orchestrates per-organization migration with automatic filtering.

**What it does:**
- Looks up organization token in `org.txt`
- Updates `TARGET_AAP_TOKEN` in `.env`
- Filters exports (if exports directory exists)
- Starts container with filtered exports mounted
- Drops you into interactive shell

**Usage:**
```bash
# Basic usage
./org-migration.sh --organization "Engineering"

# Custom env file
./org-migration.sh --organization "Sales" --env-file container/test/.env

# Inside container, then run:
aap-bridge migrate --skip-export -r projects,inventories
```

**What gets mounted:**
- `exports-<org>/` → `/app/aap-bridge/exports` (filtered data)
- `.env` → `/app/aap-bridge/.env` (org-specific TARGET token)
- `logs/`, `xformed/`, `database/`, `credential_decrypt/`

---

## Complete Migration Workflow

### Scenario: Migrate 100+ organizations with 10,000+ resources from AAP 2.4 to AAP 2.6

**Recommended:** Use **Strategy 3 (Phased Export)** for optimal performance and early validation.

---

#### Phase 1: Platform Export & Import (5-15 minutes) ⚡

**Step 1: Export platform resources (2-5 minutes)**
```bash
# Set source AAP credentials in .env
echo "SOURCE_AAP_URL=https://source-aap.example.com" >> .env
echo "SOURCE_AAP_TOKEN=<source_system_admin_token>" >> .env
echo "TARGET_AAP_URL=https://target-aap.example.com" >> .env

# Export only platform resources (100-500 objects)
echo "⏰ $(date): Starting Phase 1 export..."
time aap-bridge export -r organizations,credential_types,execution_environments
```

**Step 2: Import platform resources (2-5 minutes)**
```bash
echo "⏰ $(date): Starting Phase 1 import..."
time aap-bridge import -r organizations,credential_types,execution_environments
```

**Step 3: Create organization admin users on target (2-5 minutes)**
```bash
# Set target AAP credentials
export SOURCE_AAP_URL=https://target-aap.example.com
export SOURCE_AAP_TOKEN=<target_system_admin_token>

echo "⏰ $(date): Creating org admin users..."
./create-org-admins.sh
```

**Step 4: Generate API tokens for org admins**
```bash
echo "⏰ $(date): Generating org admin tokens..."
./create-org-tokens.sh --target
# Creates org.txt with target org admin tokens
```

**✅ MILESTONE: Platform ready in ~15 minutes! Can start testing with one org.**

---

#### Phase 2: Content Export (30-90 minutes, runs in background)

**Step 5: Export org-specific content (can run while testing Phase 1)**
```bash
echo "⏰ $(date): Starting Phase 2 export (background)..."

# Export org-specific resources (9,000+ objects)
# Run in background so you can start org migrations
nohup aap-bridge export \
  -r credentials,projects,inventories,job_templates,workflow_job_templates,teams,schedules,users \
  > phase2-export.log 2>&1 &

# Monitor progress (optional)
tail -f phase2-export.log

# Or run in foreground if preferred:
# time aap-bridge export -r credentials,projects,inventories,job_templates,workflow_job_templates,teams,schedules,users
```

**⏰ This takes 30-90 minutes for 10,000 objects, but Phase 1 is already complete!**

---

#### Phase 3: Per-Organization Migration (5-10 minutes per org)

**Wait for Phase 2 export to complete, then migrate each organization:**

```bash
# Check if Phase 2 export is complete
tail phase2-export.log  # Look for "Export complete" or similar

# Once complete, start org migrations
echo "⏰ $(date): Starting org-based migrations..."
```

**For each organization:**

```bash
# 1. Run org migration script
./org-migration.sh --organization "Engineering"

# Script automatically:
# ✓ Looks up Engineering's token in org.txt
# ✓ Updates TARGET_AAP_TOKEN with Engineering org admin token
# ✓ Filters exports/ → exports-Engineering/ (only Engineering's data)
# ✓ Starts container with filtered exports mounted

# 2. Inside container, run migration (org admin token)
aap-bridge migrate --skip-export \
  -r credentials,projects,inventories,job_templates,workflow_job_templates,teams,schedules

# 3. Verify results
aap-bridge migration-report

# 4. Exit container
exit
```

**Repeat for each org (can run in parallel on multiple machines):**
```bash
# Marketing
./org-migration.sh --organization "Marketing"
# Inside: aap-bridge migrate --skip-export -r credentials,projects,inventories,job_templates,workflow_job_templates,teams,schedules

# Sales
./org-migration.sh --organization "Sales"
# Inside: Same command

# IT Operations
./org-migration.sh --organization "IT_Operations"
# Inside: Same command

# ... repeat for all 100 organizations
```

**Automate with a loop (optional):**
```bash
#!/bin/bash
# migrate-all-orgs.sh

ORGS=(
  "Engineering"
  "Marketing"
  "Sales"
  "IT_Operations"
  "Finance"
  # ... add all 100 orgs
)

for org in "${ORGS[@]}"; do
  echo "========================================="
  echo "⏰ $(date): Migrating organization: $org"
  echo "========================================="
  
  # Start container and migrate
  # NOTE: This requires manual migration inside container
  # For full automation, see Advanced Usage section
  ./org-migration.sh --organization "$org"
  
  echo "✅ Completed: $org"
  echo ""
done
```

---

## File Structure

```
aap-migration-tool/
├── create-org-admins.sh          # Create org admin users
├── create-org-tokens.sh          # Generate API tokens
├── filter-org-exports.sh         # Filter exports by org
├── org-migration.sh              # Orchestrate per-org migration
├── org.txt                       # Org → Token mapping (gitignored)
├── .env                          # AAP credentials (gitignored)
├── exports/                      # Full export (all orgs)
├── exports-Engineering/          # Filtered export (Engineering only)
├── exports-Marketing/            # Filtered export (Marketing only)
├── logs/                         # Migration logs
├── xformed/                      # Transformed data
└── database/                     # Migration state database
```

---

## org.txt Format

```bash
# Organization Token Mapping
# Format: organization_name=api_token
# Generated by create-org-tokens.sh

Engineering=$ENGINEERING_TOKEN
Marketing=$MARKETING_TOKEN
Sales=$SALES_TOKEN
IT_Operations=$IT_OPS_TOKEN

# Default organization uses system admin token
# Default=$SYSTEM_ADMIN_TOKEN
```

**Notes:**
- Organization names are sanitized: spaces → underscores, special chars removed
- Tokens are TARGET AAP org admin tokens (created with --target flag)
- Keep this file secure (added to .gitignore)

---

## Troubleshooting

### "Organization not found in org.txt"
```bash
# List available organizations
grep -E '^[^#].*=' org.txt | cut -d'=' -f1

# Regenerate org.txt
./create-org-tokens.sh --target
```

### "Failed to authenticate as admin_<org_name>"
```bash
# Verify user exists
curl -sk -u admin:password https://target/api/v2/users/?username=admin_engineering

# Recreate org admins
./create-org-admins.sh
```

### "Exports directory not found"
```bash
# Run full export first
aap-bridge export

# Then run org migration
./org-migration.sh --organization "Engineering"
```

### "Container image not found"
```bash
# Build container
podman build -t localhost/cto-final-test -f Containerfile .

# Or set custom image
export CONTAINER_NAME=localhost/myimage:latest
./org-migration.sh --organization "Engineering"
```

---

## Security Notes

**Credentials:**
- `org.txt` contains API tokens → **NEVER commit to git** (in .gitignore)
- `.env` contains AAP credentials → **NEVER commit to git** (in .gitignore)
- Password `ansible123` is hardcoded → change in production

**Token Scope:**
- Organization admin tokens have **write access** to their organization only
- Cannot create new organizations (requires system admin)
- Cannot access other organizations' resources

**Backups:**
- Script creates `.env.org-backup` before modifying `.env`
- Filtered exports do not modify original `exports/` directory

---

## Advanced Usage

### Custom Container Image
```bash
export CONTAINER_NAME=localhost/my-aap-bridge:v2
./org-migration.sh --organization "Engineering"
```

### Custom Exports Directory
```bash
# Filter custom exports location
./filter-org-exports.sh --organization "Sales" --exports-dir /path/to/exports

# Then manually mount in container
```

### Batch Migration Script
```bash
#!/bin/bash
# migrate-all-orgs.sh

ORGS=("Engineering" "Marketing" "Sales" "IT Operations")

for org in "${ORGS[@]}"; do
    echo "=== Migrating: $org ==="
    ./org-migration.sh --organization "$org"
    # Inside container: aap-bridge migrate --skip-export -r all
    # exit
done
```

---

## FAQ

**Q: Do I need to export for each organization?**  
A: No! Export once with system admin, then filter per-org with `filter-org-exports.sh`.

**Q: Should I use full export or phased export?**  
A: Use **phased export (Strategy 3)** if you have 10,000+ objects. It's 9X faster to first usable org (15 min vs 135 min).

**Q: Can I migrate multiple orgs in parallel?**  
A: Yes, but ensure each has its own database/xformed directory or use separate containers.

**Q: What if Phase 2 export fails?**  
A: Phase 1 (platform) is already imported and working. Just retry Phase 2 export. Organizations are ready for testing.

**Q: What if an organization has no resources?**  
A: The filter script will create an empty filtered export. Migration will succeed with 0 resources.

**Q: How do I migrate 1000+ organizations efficiently?**  
A: Use phased export. Export once (Phases 1+2), then loop through orgs with `org-migration.sh`. Each migration uses filtered exports (5-10 min per org).

**Q: Can I start org migrations while Phase 2 export is running?**  
A: Yes! After Phase 1 completes (15 min), platform is ready. You can test one org while Phase 2 exports in background. Once Phase 2 completes, migrate remaining orgs.

**Q: Which resources should I export in Phase 1 vs Phase 2?**  
A:  
- **Phase 1** (platform, fast): `organizations,credential_types,execution_environments`  
- **Phase 2** (content, slow): `credentials,projects,inventories,job_templates,workflow_job_templates,teams,schedules,users`

---

## Support

For issues or questions:
- GitHub Issues: https://github.com/arnav3000/aap-migration-tool/issues
- Docs: https://github.com/arnav3000/aap-migration-tool/tree/main/docs
