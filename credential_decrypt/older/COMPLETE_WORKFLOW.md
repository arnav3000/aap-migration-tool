# Complete AAP 2.4 → 2.6 Credential Migration Workflow

## End-to-End Process

This document provides the complete workflow for migrating AAP credentials with real secret values.

---

## Phase 1: Extract Credentials from AAP 2.4

### On AAP 2.4 Controller

```bash
# 1. Copy scripts to controller
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt
scp -r scripts root@aap24-controller:/tmp/credential_scripts

# 2. SSH to controller
ssh root@aap24-controller
cd /tmp/credential_scripts

# 3. Make scripts executable
chmod +x run_extraction_fixed.sh encrypt_credentials.sh

# 4. Extract credentials
./run_extraction_fixed.sh
# Output: credentials_decrypted.json with 58 credentials

# 5. Encrypt immediately (MANDATORY!)
./encrypt_credentials.sh openssl
# Enter a strong passphrase when prompted
# Output: credentials_decrypted.json.enc

# 6. Verify encrypted file exists
ls -lh credentials_decrypted.json.enc

# 7. Delete unencrypted file
shred -u credentials_decrypted.json

# 8. Verify deletion
ls -lh credentials_decrypted.json  # Should show "No such file"
```

### On Your Workstation

```bash
# 9. Transfer encrypted file from controller
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt/scripts
scp root@aap24-controller:/tmp/credential_scripts/credentials_decrypted.json.enc ./

# 10. Decrypt on workstation
./decrypt_credentials.sh openssl
# Enter the same passphrase from step 5
# Output: credentials_decrypted.json

# 11. Move to parent directory for migration
mv credentials_decrypted.json ../

# 12. Clean up controller
ssh root@aap24-controller 'shred -u /tmp/credential_scripts/credentials_decrypted.json.enc; rm -rf /tmp/credential_scripts'
```

**Result:** You now have `credential_decrypt/credentials_decrypted.json` with all 58 credentials and their real secret values.

---

## Phase 2: Run Full Migration to AAP 2.6

```bash
cd /Users/arbhati/project/git/aap-bridge-fork

# Run complete migration
python3 -m aap_migration.cli.main migrate full --config config.yml
```

**What happens:**
- Organizations, teams, users → Created in AAP 2.6
- Credential types → Mapped/created in AAP 2.6
- **Credentials → Created with `$encrypted$` placeholders** ⚠️
- Inventories, projects, job templates → Created in AAP 2.6
- Migration state database → Tracks source ID → target ID mappings

**Result:** All resources migrated, but credentials have placeholder values instead of real secrets.

---

## Phase 3: Update Credentials with Real Secrets

### Test with Dry Run First

```bash
cd credential_decrypt

python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --dry-run \
  --report credential-update-dry-run.md
```

**Review the dry-run report:**

```bash
cat credential-update-dry-run.md
```

Check:
- How many credentials will be updated?
- Are there any failures or missing mappings?
- Which secret fields will be updated?

### Apply Real Updates

If dry-run looks good:

```bash
python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --report credential-update-report.md
```

**Expected output:**

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

INFO: processing_credential source_id=1 name=SSH-Production-01
INFO: credential_updated name=SSH-Production-01 target_id=45 secret_fields=['ssh_key_data', 'ssh_key_unlock']
...
INFO: progress processed=58 total=58 updated=54 failed=0

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

## Phase 4: Verify and Clean Up

### Verify Updates in AAP 2.6 UI

1. Navigate to AAP 2.6 web interface
2. Go to **Resources → Credentials**
3. Select a credential (e.g., "SSH-Production-01")
4. Verify that secret fields are populated (not `$encrypted$`)
5. Test the credential:
   - Run a job template that uses it
   - Verify SSH connections work
   - Check vault access works

### Review Final Report

```bash
cat credential_update_report.md
```

Look for:
- ✅ All expected credentials updated?
- ✅ No critical failures?
- ✅ Secret fields listed correctly?

### Securely Delete Decrypted File

```bash
cd credential_decrypt

# Delete both decrypted and encrypted files
shred -u credentials_decrypted.json
shred -u scripts/credentials_decrypted.json.enc

# Verify deletion
ls -lh credentials_decrypted.json*
# Should show "No such file or directory"
```

---

## Complete Command Summary

Copy-paste friendly version:

```bash
# === PHASE 1: EXTRACT FROM AAP 2.4 ===

# On workstation
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt
scp -r scripts root@aap24-controller:/tmp/credential_scripts

# On AAP 2.4 controller
ssh root@aap24-controller
cd /tmp/credential_scripts
chmod +x run_extraction_fixed.sh encrypt_credentials.sh
./run_extraction_fixed.sh
./encrypt_credentials.sh openssl  # Enter passphrase
shred -u credentials_decrypted.json
exit

# Back on workstation
scp root@aap24-controller:/tmp/credential_scripts/credentials_decrypted.json.enc ./scripts/
cd scripts
./decrypt_credentials.sh openssl  # Enter same passphrase
mv credentials_decrypted.json ../
cd ..

# === PHASE 2: RUN MIGRATION ===

cd /Users/arbhati/project/git/aap-bridge-fork
python3 -m aap_migration.cli.main migrate full --config config.yml

# === PHASE 3: UPDATE CREDENTIALS ===

cd credential_decrypt

# Dry run first
python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --dry-run \
  --report credential-update-dry-run.md

# Review dry-run report
cat credential-update-dry-run.md

# Apply real updates
python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --report credential-update-report.md

# === PHASE 4: VERIFY AND CLEAN UP ===

# Review final report
cat credential-update-report.md

# Test credentials in AAP 2.6 UI

# Securely delete decrypted files
shred -u credentials_decrypted.json
shred -u scripts/credentials_decrypted.json.enc

# Clean up controller
ssh root@aap24-controller 'rm -rf /tmp/credential_scripts'
```

---

## Timeline Estimate

| Phase | Estimated Time | Notes |
|-------|---------------|-------|
| **Phase 1: Extract** | 10-15 minutes | Depends on credential count |
| **Phase 2: Migrate** | 30-60 minutes | Depends on total resources |
| **Phase 3: Update** | 5-10 minutes | Fast - just PATCH operations |
| **Phase 4: Verify** | 15-20 minutes | Manual testing in UI |
| **Total** | ~60-105 minutes | First-time run |

Re-runs are faster since migration state is preserved.

---

## Troubleshooting

### Problem: "No mapping found" for many credentials

**Cause:** Migration didn't complete successfully or state database is corrupted.

**Solution:**
```bash
# Check migration state
sqlite3 /path/to/migration_state.db "SELECT COUNT(*) FROM id_mappings WHERE resource_type='credentials';"

# If count is low/zero, re-run migration
python3 -m aap_migration.cli.main migrate full --config config.yml
```

---

### Problem: "Update failed: 400 Bad Request"

**Cause:** Invalid input data or field validation error.

**Solution:**
```bash
# Check update report for specific error messages
cat credential-update-report.md

# Common issues:
# - SSH key format issues (missing headers/footers)
# - Invalid JSON in inputs field
# - Field name mismatch between credential types
```

---

### Problem: Credentials work in UI but fail in job templates

**Cause:** SSH keys or passwords may have been corrupted during transfer.

**Solution:**
- Re-extract from AAP 2.4 (ensure no copy-paste errors)
- Verify encryption/decryption used same passphrase
- Test credential manually in AAP 2.6:
  - Resources → Credentials → Select credential → Test

---

## Files and Directories

```
credential_decrypt/
├── README.md                           # Main overview
├── UPDATE_CREDENTIALS_GUIDE.md         # ⭐ Detailed update script guide
├── COMPLETE_WORKFLOW.md                # ⭐ This file - end-to-end process
├── COPYING_GUIDE.md                    # Quick copy commands
│
├── update_credentials.py               # ⭐ Credential update script
├── credentials_decrypted.json          # Decrypted credentials (DELETE AFTER USE!)
│
├── scripts/                            # Extraction scripts
│   ├── extract_credentials_standalone.py
│   ├── run_extraction_fixed.sh
│   ├── encrypt_credentials.sh
│   └── decrypt_credentials.sh
│
└── docs/                               # Documentation
    ├── README.md
    ├── WORKFLOW.md
    └── FIXED_USAGE.md
```

---

## Security Checklist

Before completing migration, verify:

- [ ] Decrypted credentials file deleted (`shred -u`)
- [ ] Encrypted credentials file deleted (`shred -u`)
- [ ] No credentials files in `/tmp` on controller
- [ ] No credentials files in git (check `.gitignore`)
- [ ] Update reports reviewed and deleted if sensitive
- [ ] All credentials tested in AAP 2.6
- [ ] Credential comparison report shows 100% match

---

## Success Criteria

✅ All credentials from AAP 2.4 exist in AAP 2.6
✅ Secret fields populated with real values (not `$encrypted$`)
✅ Job templates run successfully with migrated credentials
✅ SSH connections work
✅ Vault access works
✅ No decrypted files remaining on disk
✅ Update report shows 0 failures

---

**Status:** ✅ Production Ready
**Version:** 1.0
**Last Updated:** 2026-04-02
**Tested On:** AAP 2.4.1 → AAP 2.6.0
