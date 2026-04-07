# Quick Setup Guide for Testing

## Prerequisites

- Access to AAP 2.4 controller (as root or awx user)
- Access to AAP 2.6 instance
- Python 3.9+ on your workstation
- aap-bridge migration tool installed
- Migration already completed (credentials exist in AAP 2.6)

---

## Step 1: Setup Configuration

```bash
# 1. Extract the toolkit
tar -xzf credential-toolkit.tar.gz
cd credential-toolkit-share

# 2. Create config file
cp config.yaml.template config.yaml

# 3. Edit config.yaml
# Update db_path to point to your migration_state.db file
# Example: db_path: sqlite:///./migration_state.db
```

---

## Step 2: Extract Credentials from AAP 2.4

```bash
# 1. Copy scripts to AAP 2.4 controller
scp -r scripts root@your-aap24-controller:/tmp/credential_scripts

# 2. SSH to AAP 2.4 controller
ssh root@your-aap24-controller
cd /tmp/credential_scripts

# 3. Run extraction
chmod +x run_extraction_fixed.sh encrypt_credentials.sh
./run_extraction_fixed.sh

# 4. Encrypt (IMPORTANT!)
./encrypt_credentials.sh openssl
# Enter a passphrase - REMEMBER IT!

# 5. Clean up
shred -u credentials_decrypted.json
exit

# 6. Back on workstation - transfer encrypted file
scp root@your-aap24-controller:/tmp/credential_scripts/credentials_decrypted.json.enc ./scripts/

# 7. Decrypt on workstation
cd scripts
./decrypt_credentials.sh openssl
# Enter the same passphrase
cd ..
```

---

## Step 3: Update Credentials in AAP 2.6

```bash
# 1. Test with dry-run first
python3 update_credentials.py \
  --config config.yaml \
  --credentials scripts/credentials_decrypted.json \
  --dry-run \
  --report test-dry-run.md

# 2. Review dry-run results
cat test-dry-run.md

# 3. If looks good, run for real
python3 update_credentials.py \
  --config config.yaml \
  --credentials scripts/credentials_decrypted.json \
  --report update-report.md

# 4. Verify results
chmod +x verify_credentials.sh
./verify_credentials.sh
```

---

## Step 4: Validation

1. **Check report:** `cat update-report.md`
2. **Verify in AAP 2.6 UI:**
   - Resources → Credentials → Pick a credential
   - Edit → Verify secret fields show `•••••` (not `$encrypted$`)
3. **Test with job template** (optional but recommended)

---

## Step 5: Cleanup

```bash
# IMPORTANT: Delete decrypted files after testing
shred -u scripts/credentials_decrypted.json
shred -u scripts/credentials_decrypted.json.enc

# Clean up controller
ssh root@your-aap24-controller 'rm -rf /tmp/credential_scripts'
```

---

## Troubleshooting

### "No mapping found" for all credentials

**Issue:** Migration state database not found or empty.

**Fix:** Ensure `config.yaml` has correct path to your migration_state.db:
```yaml
state:
  db_path: sqlite:///path/to/migration_state.db
```

### "Module not found" errors

**Issue:** Missing Python dependencies.

**Fix:** Install aap-bridge migration tool and activate its virtual environment:
```bash
cd /path/to/aap-bridge-fork
source .venv/bin/activate  # or your virtualenv
```

### Extraction fails on controller

**Issue:** Wrong user or permissions.

**Fix:** Run as awx user:
```bash
sudo su - awx
cd /tmp/credential_scripts
./run_extraction_fixed.sh
```

---

## Documentation

- **README.md** - Overview and quick start
- **UPDATE_CREDENTIALS_GUIDE.md** - Detailed update script documentation
- **COMPLETE_WORKFLOW.md** - Full end-to-end workflow
- **scripts/README.md** - Extraction scripts documentation

---

## Security Notes

⚠️ **CRITICAL:**
- Never commit `credentials_decrypted.json` to git (already in .gitignore)
- Always encrypt before transferring over network
- Use `shred -u` to delete (not just `rm`)
- Delete decrypted files immediately after testing

---

## Support

If you encounter issues:
1. Check the documentation files
2. Review log files: `scripts/extraction.log`
3. Check update report for error details

---

**Good luck with testing!** 🚀
