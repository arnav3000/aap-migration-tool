# Credential Secret Update Guide

## Overview

This guide explains how to update AAP 2.6 credentials with real secret values after migration.

**Problem:** When migrating credentials via API, secret fields return `$encrypted$` instead of real values.

**Solution:**
1. Extract real secrets from AAP 2.4 controller (using extraction scripts)
2. Run migration to AAP 2.6 (credentials created with `$encrypted$` placeholders)
3. **Use this update script to patch credentials with real secret values**

---

## Prerequisites

✅ Migration completed to AAP 2.6
✅ Decrypted credentials file from AAP 2.4 (`credentials_decrypted.json`)
✅ Migration state database exists (has source → target ID mappings)
✅ Migration config file available (`config.yml`)

---

## Complete Workflow

### Step 1: Extract Credentials from AAP 2.4

On AAP 2.4 controller:

```bash
cd /tmp/credential_scripts
./run_extraction_fixed.sh              # Extract credentials
./encrypt_credentials.sh openssl        # Encrypt (enter passphrase)
shred -u credentials_decrypted.json     # Delete unencrypted
```

Transfer to workstation:

```bash
scp root@aap24-controller:/tmp/credential_scripts/credentials_decrypted.json.enc ./
./decrypt_credentials.sh openssl        # Decrypt (enter passphrase)
```

---

### Step 2: Run Full Migration

```bash
cd /Users/arbhati/project/git/aap-bridge-fork

# Run migration (credentials will have $encrypted$ values)
python3 -m aap_migration.cli.main migrate full --config config.yml
```

This creates all resources in AAP 2.6, but credentials have placeholder values.

---

### Step 3: Update Credentials with Real Secrets

**First, test with dry-run:**

```bash
cd credential_decrypt

python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --dry-run \
  --report credential-update-dry-run.md
```

Review the report:

```bash
cat credential-update-dry-run.md
```

**If dry-run looks good, run for real:**

```bash
python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --report credential-update-report.md
```

---

### Step 4: Verify and Clean Up

```bash
# Review final report
cat credential-update-report.md

# Test a credential in AAP 2.6 UI
# Navigate to Credentials → Select a credential → Verify secrets are populated

# Securely delete decrypted file
shred -u credentials_decrypted.json
shred -u credentials_decrypted.json.enc
```

---

## What the Script Does

### 1. **Loads Decrypted Credentials**
   - Reads `credentials_decrypted.json` from AAP 2.4 extraction
   - Validates file format

### 2. **Maps Source to Target IDs**
   - Reads migration state database
   - Gets target credential ID for each source credential
   - Skips credentials with no mapping

### 3. **Extracts Secret Fields**
   - Identifies secret fields: `password`, `ssh_key_data`, `vault_password`, `token`, etc.
   - Filters out non-secret fields (username, host, etc.)
   - Ignores empty or `$encrypted$` values

### 4. **Updates Credentials**
   - PATCHes each credential's `inputs` field
   - Only updates secret fields (leaves other fields unchanged)
   - Logs each update

### 5. **Generates Report**
   - Lists all updated credentials
   - Shows which secret fields were updated
   - Reports failures and missing mappings

---

## Script Options

```bash
python3 update_credentials.py --help
```

**Required:**
- `--config, -c` : Path to migration config file (config.yml)
- `--credentials` : Path to decrypted credentials file

**Optional:**
- `--dry-run` : Test without making changes
- `--report, -r` : Output report path (default: ./credential-update-report.md)

---

## Example Output

```
======================================================================
AAP Credential Secret Update
======================================================================
Config: ../config.yml
Credentials: credentials_decrypted.json
Mode: LIVE UPDATE
Report: credential-update-report.md
======================================================================

Loading configuration...
Connecting to migration state database...
Connecting to AAP 2.6...
Loading decrypted credentials...
✓ Loaded 58 credentials

Updating credentials...

INFO:aap_migration.credential_update:processing_credential source_id=1 name=SSH-Production-01
INFO:aap_migration.credential_update:credential_updated name=SSH-Production-01 source_id=1 target_id=45 secret_fields=['ssh_key_data', 'ssh_key_unlock', 'password']
...
INFO:aap_migration.credential_update:progress processed=10 total=58 updated=10 failed=0
...

✓ Report saved: credential-update-report.md

======================================================================
SUMMARY
======================================================================
Total Credentials:     58
Updated:               54
Failed:                0
No Mapping:            2
No Secrets:            1
Managed (Skipped):     1
======================================================================

✓ All credentials updated successfully!

IMPORTANT: Securely delete the decrypted file:
  shred -u credentials_decrypted.json
```

---

## Understanding the Report

The generated markdown report includes:

### Summary Section
- Total credentials processed
- How many were updated, failed, skipped

### Updated Credentials Table
- Lists each credential with:
  - Source ID (from AAP 2.4)
  - Target ID (in AAP 2.6)
  - Credential name
  - Which secret fields were updated
  - Status message

### Failed Updates Section
- Credentials that couldn't be updated
- Error messages

### No Mapping Section
- Credentials not found in migration state
- Usually means they weren't migrated (e.g., managed credentials)

---

## Troubleshooting

### "No mapping found in migration state"

**Cause:** Credential wasn't migrated or migration state is missing.

**Solution:**
- Check if credential exists in AAP 2.6
- Verify migration completed successfully
- Check migration state database: `migration_state.db`

---

### "Update failed: 403 Forbidden"

**Cause:** Managed (system-created) credential, or insufficient permissions.

**Solution:**
- Managed credentials are automatically skipped
- Verify AAP 2.6 credentials in config have admin access

---

### "No secret fields to update"

**Cause:** Credential has no secret inputs or all are empty.

**Solution:**
- This is normal for some credential types
- Verify the credential in AAP 2.4 actually had secrets

---

### Update fails with "Connection refused"

**Cause:** Can't connect to AAP 2.6 target.

**Solution:**
- Verify AAP 2.6 is running
- Check `config.yml` has correct target URL
- Test: `curl -k https://aap26-controller/api/v2/ping/`

---

## Security Notes

⚠️ **IMPORTANT:**

1. **Keep decrypted file secure:**
   - Never commit to git (already in .gitignore)
   - Don't transfer unencrypted over network
   - Delete immediately after use

2. **Delete after migration:**
   ```bash
   shred -u credentials_decrypted.json
   shred -u credentials_decrypted.json.enc
   ```

3. **Verify updates:**
   - Test credentials in AAP 2.6 UI
   - Run a test job template
   - Verify SSH connections work

---

## Integration with Migration Workflow

This script is designed to run **after** the main migration:

```
┌─────────────────────────────────────────────────────────────┐
│ Phase 1: Extract Credentials from AAP 2.4                  │
│   - Run extraction scripts on AAP 2.4 controller           │
│   - Encrypt and transfer to workstation                    │
│   - Decrypt on workstation                                 │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 2: Run Full Migration                                │
│   - python3 -m aap_migration.cli.main migrate full         │
│   - Credentials created with $encrypted$ placeholders      │
│   - Migration state database tracks ID mappings            │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 3: Update Credentials (THIS SCRIPT)                  │
│   - python3 update_credentials.py                          │
│   - PATCHes credentials with real secret values            │
│   - Uses migration state for ID mappings                   │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ Phase 4: Verify and Clean Up                               │
│   - Test credentials in AAP 2.6                            │
│   - Shred decrypted files                                  │
│   - Review update report                                   │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Reference

```bash
# 1. Extract from AAP 2.4 (on controller)
cd /tmp/credential_scripts
./run_extraction_fixed.sh
./encrypt_credentials.sh openssl
scp credentials_decrypted.json.enc user@workstation:/path/

# 2. Decrypt on workstation
./decrypt_credentials.sh openssl

# 3. Run migration
python3 -m aap_migration.cli.main migrate full --config config.yml

# 4. Update credentials (dry-run first)
python3 update_credentials.py --config config.yml --credentials credentials_decrypted.json --dry-run

# 5. Update credentials (for real)
python3 update_credentials.py --config config.yml --credentials credentials_decrypted.json

# 6. Clean up
shred -u credentials_decrypted.json*
```

---

**Status:** ✅ Production Ready
**Version:** 1.0
**Last Updated:** 2026-04-02
