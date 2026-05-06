# Selective Migration Guide

**Last Updated**: 2026-03-24

This guide shows how to migrate specific resource types instead of migrating everything.

---

## 🎯 Quick Examples

### Migrate Only Job Templates
```bash
# Full pipeline
aap-bridge migrate -r job_templates --skip-prep

# Or step-by-step
aap-bridge export -r job_templates
aap-bridge transform -r job_templates
aap-bridge import -r job_templates
```

### Migrate Only Organizations
```bash
aap-bridge migrate -r organizations --skip-prep
```

### Migrate Only Inventories and Hosts
```bash
aap-bridge migrate -r inventories -r hosts --skip-prep
```

### Migrate Only Credentials
```bash
aap-bridge migrate -r credentials --skip-prep
```

---

## 📋 Available Resource Types

### Core Resources
- `organizations` - Organizations
- `teams` - Teams
- `users` - Users

### Credentials & Execution
- `credentials` - Credentials
- `credential_types` - Credential Types
- `execution_environments` - Execution Environments

### Projects & SCM
- `projects` - Projects (SCM repositories)

### Inventories
- `inventory` - Inventories
- `groups` - Inventory Groups
- `hosts` - Hosts
- `inventory_sources` - Dynamic Inventory Sources

### Automation
- `job_templates` - Job Templates
- `workflow_job_templates` - Workflow Job Templates
- `schedules` - Schedules
- `notification_templates` - Notification Templates

### Settings & Apps
- `settings` - AAP Settings
- `applications` - OAuth Applications
- `labels` - Labels

### Infrastructure
- `instances` - Controller Instances
- `instance_groups` - Instance Groups

### System
- `system_job_templates` - System Job Templates (cleanup, etc.)

---

## 🔗 Automatic Dependency Resolution

**The tool automatically imports dependencies!**

When you import a resource type, all required dependencies are automatically imported first.

### Example: Job Templates

```bash
# You run:
aap-bridge import -r job_templates

# Tool automatically imports (in order):
# 1. organizations
# 2. users
# 3. teams
# 4. credential_types
# 5. credentials
# 6. execution_environments
# 7. projects
# 8. inventories
# 9. job_templates ← your requested type
```

### Check Dependencies Before Importing

```bash
# See what will be imported (dry run)
aap-bridge import -r job_templates --check-dependencies

# Example output:
# ℹ Requested types: job_templates
# ℹ With dependencies: organizations, users, teams, credential_types,
#   credentials, execution_environments, projects, inventories, job_templates
```

---

## 🎨 Common Use Cases

### Use Case 1: Migrate Only Configuration (No Jobs)
```bash
# Organizations, teams, users, credentials
aap-bridge migrate \
  -r organizations \
  -r teams \
  -r users \
  -r credential_types \
  -r credentials \
  --skip-prep
```

### Use Case 2: Migrate Only Automation (Job Templates + Workflows)
```bash
# Job templates and workflows (dependencies auto-imported)
aap-bridge migrate \
  -r job_templates \
  -r workflow_job_templates \
  -r schedules \
  --skip-prep
```

### Use Case 3: Migrate Only Inventories
```bash
# Inventories with groups and hosts
aap-bridge migrate \
  -r inventory \
  -r groups \
  -r hosts \
  --skip-prep
```

### Use Case 4: Migrate Settings Only
```bash
# Just AAP settings (no resources)
aap-bridge migrate -r settings --skip-prep
```

### Use Case 5: Re-import After Fixing Issues
```bash
# Force re-import of failed credentials
aap-bridge import -r credentials --force-reimport --yes
```

---

## ⚙️ Step-by-Step Workflow

### 1. Export Specific Types
```bash
# Export only what you need
aap-bridge export -r organizations -r projects -r job_templates

# Result: Only creates these directories:
# exports/organizations/
# exports/projects/
# exports/job_templates/
```

### 2. Transform Specific Types
```bash
# Transform only exported types
aap-bridge transform -r organizations -r projects -r job_templates

# Result: Only creates these directories:
# xformed/organizations/
# xformed/projects/
# xformed/job_templates/
```

### 3. Import Specific Types
```bash
# Import with dependency resolution
aap-bridge import -r job_templates --yes

# Or skip dependencies (advanced - may fail)
aap-bridge import -r job_templates --skip-dependencies
```

---

## 🔄 Resume and Force Options

### Resume Interrupted Migration
```bash
# Resume export from checkpoint (skips already-exported)
aap-bridge export -r job_templates --resume

# Resume import from checkpoint (skips already-imported)
aap-bridge import -r job_templates --resume
```

### Force Re-export/Re-import
```bash
# Force re-export (overwrite existing exports)
aap-bridge export -r job_templates --force

# Force re-import (clear progress and re-import)
aap-bridge import -r job_templates --force-reimport
```

---

## 📊 Check Migration Status

### View Overall Status
```bash
aap-bridge migrate status
```

### Check Specific Type Status
```bash
# Check database for job templates
sqlite3 migration_state.db "
  SELECT COUNT(*) as total,
         SUM(CASE WHEN target_id IS NOT NULL THEN 1 ELSE 0 END) as imported,
         SUM(CASE WHEN target_id IS NULL THEN 1 ELSE 0 END) as failed
  FROM id_mappings
  WHERE resource_type='job_templates';
"
```

---

## 🎯 Advanced Patterns

### Pattern 1: Incremental Migration
```bash
# Day 1: Migrate foundation
aap-bridge migrate -r organizations -r teams -r users --skip-prep

# Day 2: Migrate credentials
aap-bridge migrate -r credential_types -r credentials --skip-prep

# Day 3: Migrate projects
aap-bridge migrate -r execution_environments -r projects --skip-prep

# Day 4: Migrate automation
aap-bridge migrate -r inventories -r job_templates --skip-prep
```

### Pattern 2: Fix Specific Failures
```bash
# 1. Check what failed
sqlite3 migration_state.db "
  SELECT resource_type, COUNT(*) as failed_count
  FROM id_mappings
  WHERE target_id IS NULL
  GROUP BY resource_type;
"

# 2. Re-import only failed types
aap-bridge import -r schedules --force-reimport --yes
```

### Pattern 3: Test Import (Dry Run)
```bash
# See what would be imported without making changes
aap-bridge import -r job_templates --dry-run
```

---

## ⚠️ Important Notes

### Dependency Order
The tool handles dependencies automatically, but for reference:

**Dependency Chain**:
```
organizations
  ↓
users, teams, credential_types, execution_environments
  ↓
credentials, projects, inventories
  ↓
job_templates, workflow_job_templates
  ↓
schedules, notification_templates
```

### What Gets Skipped
Some resource types are intentionally NOT migrated:
- `jobs` - Job execution history (runtime data)
- `activity_stream` - Audit logs
- `metrics` - Auto-generated data
- `system_job_templates` - Built-in AAP templates

See `WHAT-IS-NOT-MIGRATED.md` for complete list.

### Settings Migration
```bash
# Settings require manual review
aap-bridge migrate -r settings --skip-prep

# Then review generated report:
cat SETTINGS-REVIEW-REPORT.md
```

---

## 🔍 Troubleshooting

### "No such resource type"
```bash
# List available types
cat schemas/source_endpoints.json | jq -r '.endpoints | keys[]' | sort
```

### "Dependency not found"
```bash
# Let tool import dependencies automatically (recommended)
aap-bridge import -r job_templates

# Or import dependencies manually first
aap-bridge import -r organizations
aap-bridge import -r projects
aap-bridge import -r job_templates --skip-dependencies
```

### "Already imported"
```bash
# Use --force-reimport to clear and re-import
aap-bridge import -r job_templates --force-reimport --yes

# Or use --resume to skip already-imported
aap-bridge import -r job_templates --resume
```

---

## 📖 Examples Summary

```bash
# Single type
aap-bridge migrate -r job_templates --skip-prep

# Multiple types
aap-bridge migrate -r organizations -r projects -r job_templates --skip-prep

# With dependency check
aap-bridge import -r job_templates --check-dependencies

# Step-by-step
aap-bridge export -r job_templates
aap-bridge transform -r job_templates
aap-bridge import -r job_templates

# Force re-import
aap-bridge import -r job_templates --force-reimport --yes

# Resume interrupted
aap-bridge import -r job_templates --resume

# Dry run
aap-bridge import -r job_templates --dry-run
```

---

## 🚀 Best Practices

1. **Start Small**: Test with a single resource type first
   ```bash
   aap-bridge migrate -r organizations --skip-prep
   ```

2. **Check Dependencies**: Use `--check-dependencies` to see what will import
   ```bash
   aap-bridge import -r job_templates --check-dependencies
   ```

3. **Use Resume**: For large migrations, use `--resume` to handle interruptions
   ```bash
   aap-bridge migrate -r job_templates --resume
   ```

4. **Verify Results**: Check migration status after each step
   ```bash
   aap-bridge migrate status
   ```

5. **Keep Logs**: Review logs for any errors
   ```bash
   tail -f logs/migration.log
   ```

---

**For more information, see:**
- `aap-bridge migrate --help`
- `aap-bridge export --help`
- `aap-bridge transform --help`
- `aap-bridge import --help`
