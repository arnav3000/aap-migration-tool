# Quick Start - Credential-First Migration

## TL;DR

```bash
# Activate environment
source .venv/bin/activate

# Run full migration (credentials automatically checked and migrated first)
aap-bridge migrate full
```

## What Changed

**Before:** Credentials migrated in Phase 3 (middle of process)
**Now:** Credentials migrated in Phase 2 (immediately after organizations)

## New Commands

```bash
# Compare credentials (shows what's missing)
aap-bridge credentials compare

# Migrate only credentials
aap-bridge credentials migrate

# Generate status report
aap-bridge credentials report
```

## Workflow

### Option 1: Full Migration (Recommended)

```bash
aap-bridge migrate full
```

**What happens:**
1. ✓ Automatic credential comparison before migration
2. ✓ Console output showing missing credentials
3. ✓ Report saved to `./reports/credential-comparison.md`
4. ✓ Phase 1: Migrate organizations
5. ✓ Phase 2: Migrate credentials (including missing ones)
6. ✓ Phase 3+: Migrate everything else

### Option 2: Credentials First, Then Everything Else

```bash
# Step 1: Check what's missing
aap-bridge credentials compare

# Step 2: Review report
cat ./reports/credential-comparison.md

# Step 3: Migrate credentials only
aap-bridge credentials migrate

# Step 4: Migrate everything else
aap-bridge migrate full
```

### Option 3: Dry Run First

```bash
# Test without making changes
aap-bridge credentials migrate --dry-run

# Review what would happen
cat ./reports/credential-comparison.md

# Then run for real
aap-bridge credentials migrate
```

## Example Output

```
================================================================================
CREDENTIAL COMPARISON RESULTS
================================================================================
Source Credentials: 36
Target Credentials: 28
Missing in Target: 8

Detailed report saved to: ./reports/credential-comparison.md
================================================================================

Phase 1: Organizations → Migrating...
Phase 2: Credentials → Migrating (8 missing credentials)...
Phase 3: Credential Input Sources → Migrating...
...
```

## Important Notes

### Secrets Cannot Be Migrated

The API returns `$encrypted$` for passwords, tokens, and keys.

**After migration:**
1. Go to target AAP Web UI
2. Edit each credential
3. Update secret values manually

### Safe to Re-Run

- Already-migrated credentials are skipped
- ID mappings prevent duplicates
- Idempotent operation

## Files

**Reports:**
- `./reports/credential-comparison.md` - What's missing
- `./reports/migration-report.md` - Full migration results

**Documentation:**
- `docs/workflows/CREDENTIAL-FIRST-WORKFLOW.md` - Complete guide
- `USER-GUIDE.md` - Full user manual
- `docs/` - All documentation

## Troubleshooting

**All credentials show as missing?**
```bash
aap-bridge credentials compare  # Populates ID mappings
```

**Need to test first?**
```bash
aap-bridge credentials migrate --dry-run
```

**Want detailed logs?**
```bash
tail -f logs/migration.log
```

## Validation

Test the implementation:
```bash
python3 test_credential_workflow.py
```

Should output: `✓ ALL TESTS PASSED`

## Next Steps

1. Configure `.env` with source and target AAP credentials
2. Run `aap-bridge credentials compare` to see the diff
3. Run `aap-bridge migrate full` to migrate everything
4. Update secret values in target AAP
5. Verify with `aap-bridge credentials report`

---

**Ready to migrate!** Start with the comparison to understand what will happen.
