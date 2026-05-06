# Credential-First Migration Workflow

## Overview

The AAP Bridge migration tool now implements a **credential-first workflow** that ensures all credentials are properly migrated before any other resources. This approach prevents dependency issues and makes credential management explicit and traceable.

## Why Credential-First?

**Problem:** In previous workflows, credentials were migrated in the middle of the process (phase 3), which could cause:
- Job templates to fail if their credentials weren't migrated yet
- Projects to fail SCM sync if authentication credentials were missing
- Inventory sources to fail if cloud credentials weren't present
- Silent failures with `$encrypted$` placeholders for secret values

**Solution:** The credential-first workflow ensures:
1. All credentials are identified and compared before migration starts
2. Missing credentials are reported with full details
3. Credentials are migrated immediately after organizations (their only dependency)
4. All other resources are migrated only after credentials are confirmed present

## How It Works

### Automatic Credential Comparison

When you run `aap-bridge migrate full`, the tool automatically:

1. **Compares credentials** between source and target instances
2. **Generates a report** showing missing credentials
3. **Displays a summary** in the console
4. **Migrates credentials first** (phase 2, right after organizations)
5. **Proceeds with other resources** only after credentials are complete

### New Migration Phase Order

```
Phase 1: Organizations (foundation - credentials depend on these)
Phase 2: Credentials (CRITICAL - must complete before other resources)
Phase 3: Credential Input Sources
Phase 4: Identity (Users, Teams, Labels)
Phase 5: Execution Environments
Phase 6: Inventories
Phase 7: Hosts
Phase 8: Instances & Instance Groups
Phase 9: Projects
Phase 10: Inventory Configuration
Phase 11: Notification Templates
Phase 12: Job Templates
Phase 13: Workflows
Phase 14: System Job Templates
Phase 15: Schedules
```

## New CLI Commands

### Compare Credentials Only

Compare credentials without performing migration:

```bash
aap-bridge credentials compare

# Custom output location
aap-bridge credentials compare --output ./my-reports/creds.md
```

**Output:**
- Console summary with statistics
- Detailed markdown report with:
  - Total credential counts
  - List of missing credentials
  - Credential details (type, organization, inputs)
  - Recommendations for next steps

### Migrate Credentials Only

Migrate just the credentials (and their dependencies):

```bash
aap-bridge credentials migrate

# Dry run mode
aap-bridge credentials migrate --dry-run

# Custom report directory
aap-bridge credentials migrate --report-dir ./my-reports
```

**What it does:**
1. Compares credentials to find missing ones
2. Migrates organizations (dependency)
3. Migrates credential types (dependency)
4. Migrates credentials
5. Generates detailed migration report

### Generate Credential Report

Generate a comprehensive credential status report:

```bash
aap-bridge credentials report

# Custom output location
aap-bridge credentials report --output ./status.md
```

## Full Migration with Credential-First

The full migration process now includes automatic credential checking:

```bash
aap-bridge migrate full
```

### What Happens:

1. **Pre-Migration Credential Check:**
   ```
   ================================================================================
   CREDENTIAL COMPARISON RESULTS
   ================================================================================
   Source Credentials: 36
   Target Credentials: 28
   Missing in Target: 8

   Detailed report saved to: ./reports/credential-comparison.md
   ================================================================================
   ```

2. **Phase 1:** Migrate Organizations
3. **Phase 2:** Migrate Credentials (including the 8 missing ones)
4. **Phase 3+:** Migrate all other resources

## Understanding the Report

### Credential Comparison Report Structure

```markdown
# Credential Comparison Report

**Total Source Credentials:** 36
**Total Target Credentials:** 28
**Matching Credentials:** 28
**Missing in Target:** 8
**Managed Credentials (Skipped):** 2

## Missing Credentials

| Source ID | Name | Type | Organization | Description |
|-----------|------|------|--------------|-------------|
| 33 | Galaxy/Hub Token 47 | Galaxy Token | Global Engineering | |
| 34 | Galaxy/Hub Token 48 | Galaxy Token | IT Operations | |
...

### Details

#### 1. Galaxy/Hub Token 47
- **Source ID:** 33
- **Type:** Ansible Galaxy/Automation Hub API Token (ID: 19)
- **Organization: Global Engineering (ID: 5)
- **Description:**
- **Inputs:** `['url', 'token']` (values are encrypted)
```

## Important Notes

### Secret Values Cannot Be Migrated

The AAP API returns `$encrypted$` for all secret credential fields. This means:

**What DOES migrate:**
- Credential structure (name, type, organization)
- Non-secret fields (URLs, usernames, etc.)
- Credential associations

**What DOES NOT migrate:**
- Passwords
- API tokens
- SSH private keys
- Vault passwords
- Any encrypted values

### Post-Migration Steps

After credential migration, you may need to:

1. **Review the credential comparison report** to see which credentials were migrated
2. **Update secret values** for critical credentials:
   ```bash
   # Via API
   curl -X PATCH \
     -H "Authorization: Bearer YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"inputs": {"password": "actual_secret"}}' \
     "https://your-target-aap/api/controller/v2/credentials/123/"

   # Or via AAP Web UI
   # Navigate to: Resources → Credentials → Edit
   ```

3. **Test credentials** before running job templates
4. **Verify credential associations** with projects, inventories, and job templates

### Managed Credentials

Managed credentials (system-created) are automatically skipped:
- Demo Credential
- Ansible Galaxy
- Automation Hub repositories
- Other built-in credentials

These already exist in the target and don't need migration.

## Workflow Examples

### Example 1: First-Time Migration

```bash
# Step 1: Compare credentials to see what's missing
aap-bridge credentials compare

# Review the report at ./reports/credential-comparison.md

# Step 2: Migrate everything (credentials will be handled first)
aap-bridge migrate full

# Step 3: Verify credentials
aap-bridge credentials report
```

### Example 2: Credential-Only Migration

```bash
# If you only want to migrate credentials:
aap-bridge credentials migrate

# This will:
# - Migrate organizations (if needed)
# - Migrate credential types (if needed)
# - Migrate credentials
# - Skip all other resources
```

### Example 3: Retry After Failure

If credential migration fails:

```bash
# Step 1: Check what happened
aap-bridge credentials compare

# Step 2: Review migration report
cat ./reports/migration-report.md

# Step 3: Retry credential migration
aap-bridge credentials migrate

# Or resume full migration (will skip already-migrated credentials)
aap-bridge migrate full
```

## Advanced: Customizing Credential Migration

### Skip Credential Comparison

If you want to skip the pre-migration credential check:

```bash
# Migrate without credential comparison
aap-bridge migrate full --skip-phases credentials

# Note: This is NOT recommended and may cause dependency issues
```

### Migrate Only Missing Credentials

The tool automatically detects and migrates only missing credentials through:
- ID mapping in the state database
- Comparison by (name, type, organization) tuple
- Idempotency checks before creation

## Troubleshooting

### Issue: All Credentials Show as Missing

**Cause:** State database may not have credential mappings

**Solution:**
```bash
# Clear state and re-run comparison
aap-bridge state clear --resource-type credentials
aap-bridge credentials compare
```

### Issue: Credentials Migrated But Secrets Don't Work

**Cause:** Secret values return `$encrypted$` from API

**Solution:**
1. Update secret values manually in target AAP
2. Use Vault-backed credentials if possible
3. Use credential input sources for external secret management

### Issue: Credential Type Mismatch Errors

**Cause:** Credential types have different IDs between source and target

**Solution:**
The tool automatically maps credential types by name. If errors occur:
```bash
# Check credential type mappings
aap-bridge state list --resource-type credential_types

# Re-run with fresh credential type mapping
aap-bridge migrate full --skip-phases credentials
aap-bridge credentials migrate
```

## Best Practices

1. **Always run comparison first:** Understand what needs to be migrated before starting
2. **Review reports:** Check credential comparison and migration reports for issues
3. **Update secrets post-migration:** Plan time to update credential secrets
4. **Test credentials:** Verify credentials work before running job templates
5. **Use dry-run mode:** Test the process with `--dry-run` before actual migration
6. **Keep reports:** Save credential comparison reports for audit trails

## API Reference

### Credential Comparator

The credential comparison is powered by the `CredentialComparator` class:

```python
from aap_migration.migration.credential_comparator import CredentialComparator

comparator = CredentialComparator(
    source_client=source_client,
    target_client=target_client,
    state=state,
)

# Compare credentials
result = await comparator.compare_credentials()

# Generate report
report = comparator.generate_report(result)
```

### Coordinator Integration

The `MigrationCoordinator` includes credential comparison:

```python
from aap_migration.migration.coordinator import MigrationCoordinator

coordinator = MigrationCoordinator(...)

# Compare and verify credentials
summary = await coordinator.compare_and_verify_credentials(
    report_path="./credential-comparison.md"
)

# Returns:
# {
#     "total_source": 36,
#     "total_target": 28,
#     "missing_count": 8,
#     "missing_credentials": [...],
#     "report": "..."
# }
```

## Migration Reports

### Report Locations

All reports are saved to `./reports/` by default:

- `credential-comparison.md` - Pre-migration credential comparison
- `migration-report.md` - Full migration summary
- `migration-report.json` - Machine-readable migration data
- `migration-report.html` - HTML visualization

### Report Contents

**Credential Comparison Report:**
- Total counts
- Missing credentials with details
- Recommendations

**Migration Report (credentials section):**
- Credentials exported
- Credentials imported
- Credentials failed
- Credentials skipped
- Error details

## See Also

- [Migration Workflow](./docs/user-guide/migration-workflow.md)
- [Credential Migration Limitation](./docs/CREDENTIAL-MIGRATION-LIMITATION.md)
- [Credential Comparison Report](./CREDENTIAL-COMPARISON-REPORT.md)
- [Configuration Guide](./docs/getting-started/configuration.md)
