# Credential Decryption for AAP 2.4 Migration

## Problem Statement

When exporting credentials from AAP 2.4, secret fields (passwords, SSH keys, tokens, etc.) are **encrypted** and cannot be migrated directly to AAP 2.6. The `$encrypted$` placeholder prevents actual secret values from being imported.

## Solution

This toolkit extracts and **decrypts ALL credentials** from the AAP 2.4 controller database, creating a JSON mapping file with real secret values that can be used during migration.

---

## 📁 Files

| File | Purpose |
|------|---------|
| `extract_all_credentials.py` | Python script that connects to AAP 2.4 database and decrypts all credentials |
| `run_extraction.sh` | Wrapper script to execute extraction on AAP 2.4 controller |
| `credentials_decrypted.json` | **OUTPUT** - JSON file with decrypted credential data (created after running) |
| `extraction.log` | **OUTPUT** - Execution log with progress details |

---

## 🚀 Usage

### Step 1: Copy Scripts to AAP 2.4 Controller

```bash
# On your workstation (where aap-bridge is installed)
cd /path/to/aap-bridge-fork/credential_decrypt

# Copy to AAP 2.4 controller (replace with your controller hostname)
scp extract_all_credentials.py run_extraction.sh root@aap24-controller:/tmp/
```

### Step 2: Run Extraction on AAP 2.4 Controller

```bash
# SSH to AAP 2.4 controller
ssh root@aap24-controller

# Navigate to extraction directory
cd /tmp

# Make script executable (if not already)
chmod +x run_extraction.sh

# Run the extraction
./run_extraction.sh
```

**Output:**
```
========================================
AAP 2.4 Credential Extraction
========================================

→ Running extraction script via awx-manage shell_plus...
→ This may take a few minutes depending on credential count...

[1/57] Processing: SSH-Prod-01 (ID: 1, Type: Machine)
  ✓ Decrypted field: password
  ✓ Decrypted field: ssh_key_data
[2/57] Processing: Vault-Password (ID: 2, Type: Vault)
  ✓ Decrypted field: vault_password
...

✓ Extraction completed successfully!

Results:
  Credentials extracted: 57
  Errors: 0

Output files:
  JSON data: /tmp/credentials_decrypted.json
  Logs:      /tmp/extraction.log

⚠️  WARNING: This file contains UNENCRYPTED SECRETS!
   Keep it secure and delete after migration.
```

### Step 3: Encrypt the File (MANDATORY - On AAP 2.4 Controller)

**🔒 CRITICAL:** Never transfer unencrypted credential files over the network!

```bash
# Still on AAP 2.4 controller
cd /tmp

# Copy encryption script if not already there
# (Should have been copied in Step 1)

# Make script executable
chmod +x encrypt_credentials.sh

# Encrypt using GPG (RECOMMENDED - Strong AES256 encryption)
./encrypt_credentials.sh gpg

# You will be prompted for a passphrase - REMEMBER IT!
# Output: credentials_decrypted.json.gpg

# OR use base64 encoding (NOT RECOMMENDED - Only obfuscation, not encryption)
./encrypt_credentials.sh base64
# Output: credentials_decrypted.json.b64
```

**Output:**
```
========================================
Credential File Encryption
========================================

→ Input file: /tmp/credentials_decrypted.json
→ Encryption method: gpg

Using GPG symmetric encryption (AES256)
You will be prompted to enter a passphrase
⚠️  Remember this passphrase - you'll need it to decrypt!

Enter passphrase: ****
Repeat passphrase: ****

✓ File encrypted successfully!
→ Encrypted file: /tmp/credentials_decrypted.json.gpg

File sizes:
  Original:  245K
  Encrypted: 189K

✓ File is properly encrypted (PGP/GPG format)
```

### Step 4: Securely Delete Original & Transfer Encrypted File

```bash
# IMPORTANT: Securely delete the unencrypted original
shred -u /tmp/credentials_decrypted.json

# Transfer ONLY the encrypted file to workstation
scp /tmp/credentials_decrypted.json.gpg user@workstation:/path/to/credential_decrypt/

# Clean up on controller
shred -u /tmp/credentials_decrypted.json.gpg
rm -f /tmp/extraction.log
```

### Step 5: Decrypt on Workstation

```bash
# On your workstation
cd /path/to/aap-bridge-fork/credential_decrypt

# Make decrypt script executable
chmod +x decrypt_credentials.sh

# Decrypt the file (you'll be prompted for passphrase)
./decrypt_credentials.sh gpg

# For base64:
# ./decrypt_credentials.sh base64

# Output: credentials_decrypted.json (with restrictive 400 permissions)
```

**Output:**
```
========================================
Credential File Decryption
========================================

→ Input file: credentials_decrypted.json.gpg
→ Decryption method: gpg
→ Output file: credentials_decrypted.json

Using GPG decryption
You will be prompted to enter the passphrase

Enter passphrase: ****

✓ File decrypted successfully!
→ Decrypted file: credentials_decrypted.json

Verifying JSON format...
✓ Valid JSON file

Credentials Summary:
  Total credentials: 57
  First 5 credentials:
    - SSH-Production-Servers (Machine)
    - Ansible-Vault-Production (Vault)
    - AWS-Production-Account (Amazon Web Services)
    ...

✓ Set permissions to 400 (read-only for owner)
```

### Step 6: Use for Migration & Clean Up

```bash
# Use the decrypted data during migration
# (See integration sections below)

# After migration is complete, SECURELY DELETE:
shred -u credential_decrypt/credentials_decrypted.json
shred -u credential_decrypt/credentials_decrypted.json.gpg  # Optional
```

---

## 📊 Output Format

The `credentials_decrypted.json` file contains:

```json
{
  "metadata": {
    "extraction_date": "2026-04-02T10:30:00",
    "total_credentials": 57,
    "processed_count": 57,
    "error_count": 0,
    "source_system": "AAP 2.4"
  },
  "credentials": [
    {
      "id": 1,
      "name": "SSH-Prod-01",
      "description": "Production SSH credential",
      "credential_type": "Machine",
      "credential_type_id": 1,
      "credential_type_kind": "ssh",
      "organization": "IT Operations",
      "organization_id": 2,
      "inputs": {
        "username": "ansible",
        "password": "REAL_DECRYPTED_PASSWORD_HERE",
        "ssh_key_data": "-----BEGIN RSA PRIVATE KEY-----\nREAL_PRIVATE_KEY_DATA_HERE\n-----END RSA PRIVATE KEY-----",
        "ssh_key_unlock": "REAL_PASSPHRASE_HERE"
      },
      "created": "2024-01-15T10:30:00",
      "modified": "2025-12-20T14:20:00"
    },
    {
      "id": 2,
      "name": "Vault-Password",
      "description": "Ansible Vault password",
      "credential_type": "Vault",
      "credential_type_id": 3,
      "credential_type_kind": "vault",
      "organization": "IT Operations",
      "organization_id": 2,
      "inputs": {
        "vault_password": "REAL_VAULT_PASSWORD_HERE",
        "vault_id": "production"
      }
    }
  ]
}
```

---

## 🔐 Security Considerations

### ⚠️ CRITICAL WARNINGS

1. **Contains Real Secrets**: The JSON file contains **UNENCRYPTED passwords, SSH keys, API tokens**, etc.
2. **High-Value Target**: This file is extremely sensitive - treat it like your database backup
3. **Access Control**: Only authorized personnel should handle this file
4. **Temporary**: Delete immediately after migration is complete

### Best Practices

✅ **Do:**
- Run extraction during a maintenance window
- Use encrypted transfer (scp, sftp)
- Store in encrypted filesystem or encrypt the file itself
- Delete from AAP 2.4 controller after copying
- Delete from workstation after migration
- Use restrictive file permissions (chmod 400)
- Audit who accessed the file

❌ **Don't:**
- Email or store in unencrypted cloud storage
- Commit to version control (git)
- Leave on shared/public systems
- Share via messaging apps
- Keep longer than necessary

### Recommended Secure Workflow

```bash
# ===== ON AAP 2.4 CONTROLLER =====

# 1. Extract credentials
./run_extraction.sh

# 2. Encrypt BEFORE transfer (MANDATORY!)
./encrypt_credentials.sh gpg
# Enter passphrase when prompted

# 3. Immediately delete unencrypted original
shred -u /tmp/credentials_decrypted.json

# ===== TRANSFER =====

# 4. Copy ONLY encrypted file to workstation
scp /tmp/credentials_decrypted.json.gpg user@workstation:/path/to/credential_decrypt/

# 5. Clean up controller
shred -u /tmp/credentials_decrypted.json.gpg
rm -f /tmp/extraction.log

# ===== ON WORKSTATION =====

# 6. Decrypt for use
./decrypt_credentials.sh gpg
# Enter passphrase when prompted

# 7. Use during migration
# ... reference credentials_decrypted.json ...

# 8. After migration, securely delete BOTH files
shred -u credentials_decrypted.json
shred -u credentials_decrypted.json.gpg
```

**Why This Workflow?**

✅ **Never transfers unencrypted secrets** over network
✅ **Strong AES256 encryption** with passphrase
✅ **Immediate cleanup** of unencrypted data
✅ **Secure deletion** (shred, not rm)
✅ **Minimal exposure window** for sensitive data

---

## 🔄 Integration with AAP Bridge

### Manual Approach (Current)

During migration, manually update credentials in AAP 2.6 using the decrypted values:

1. Import credentials normally (they'll have `$encrypted$` values)
2. Look up real values in `credentials_decrypted.json`
3. Update credentials in AAP 2.6 with real values

### Automated Approach (Future Enhancement)

Create an import script that:
1. Reads `credentials_decrypted.json`
2. Imports credentials to AAP 2.6 with real secret values
3. Bypasses the encrypted field limitation

---

## 🐛 Troubleshooting

### Error: "awx-manage command not found"

**Cause:** Script not run on AAP 2.4 controller node

**Fix:**
```bash
# This script MUST be run on the controller node, not your workstation
ssh root@aap24-controller
./run_extraction.sh
```

### Error: "Permission denied"

**Cause:** Insufficient permissions

**Fix:**
```bash
# Run as root or awx user
sudo ./run_extraction.sh

# Or switch user
sudo su - awx
cd /tmp
./run_extraction.sh
```

### Error: "Could not decrypt field"

**Cause:** Field encryption key not accessible

**Fix:**
- Ensure you're running as proper user (awx or root)
- Check AAP installation is healthy
- Verify `settings.py` has correct encryption keys

### Partial Results (Some Credentials Missing)

**Check:** Review `extraction.log` for error messages

**Common causes:**
- Credential type not found
- Organization deleted but credential remains
- Database inconsistency

---

## 📝 Example Use Case

### Scenario: Migrating 57 Credentials

**Before:**
```bash
# Export from AAP 2.4
aap-bridge export

# Import to AAP 2.6
aap-bridge import
# Result: All credentials imported but with $encrypted$ values ❌
```

**With Credential Decrypt:**
```bash
# 1. Extract decrypted credentials from AAP 2.4
./run_extraction.sh on AAP 2.4 controller

# 2. Get credentials_decrypted.json file

# 3. During migration, reference this file to update credentials with real values

# 4. All 57 credentials now have real secret data ✅
```

---

## 🛠️ Advanced: Credential Types Supported

The script automatically handles ALL credential types including:

- **Machine (SSH)**: username, password, ssh_key_data, ssh_key_unlock, become_password
- **Source Control**: username, password, ssh_key_data, ssh_key_unlock
- **Vault**: vault_password, vault_id
- **Network**: username, password, authorize_password, ssh_key_data
- **Amazon Web Services**: username (access_key), password (secret_key), security_token
- **Google Compute Engine**: username, project, ssh_key_data
- **Microsoft Azure**: username, password, subscription, tenant, client, secret
- **VMware vCenter**: username, password, host
- **Red Hat Satellite 6**: username, password, host
- **OpenStack**: username, password, host, project
- **Custom Credential Types**: All fields marked as `secret: true`

---

## 📚 Related Documentation

- [AAP Credential Migration Limitation](../docs/CREDENTIAL-MIGRATION-LIMITATION.md)
- [Credential First Workflow](../docs/workflows/CREDENTIAL-FIRST-WORKFLOW.md)
- [Zero-Loss Credential Migration](../docs/ZERO-LOSS-CREDENTIAL-MIGRATION.md)

---

## ⚡ Quick Reference

```bash
# === ON AAP 2.4 CONTROLLER ===
./run_extraction.sh              # Extract credentials
./encrypt_credentials.sh gpg      # Encrypt with GPG (enter passphrase)
shred -u /tmp/credentials_decrypted.json  # Delete unencrypted

# === TRANSFER ===
scp /tmp/credentials_decrypted.json.gpg user@workstation:/path/

# === CLEANUP CONTROLLER ===
shred -u /tmp/credentials_decrypted.json.gpg

# === ON WORKSTATION ===
./decrypt_credentials.sh gpg      # Decrypt (enter passphrase)

# === USE DURING MIGRATION ===
# Reference credentials_decrypted.json to update credentials in AAP 2.6

# === AFTER MIGRATION ===
shred -u credentials_decrypted.json      # Delete decrypted
shred -u credentials_decrypted.json.gpg  # Delete encrypted
```

**Encryption Methods:**
- `gpg` - **Recommended** (AES256 encryption with passphrase)
- `base64` - Not recommended (only obfuscation, not real encryption)

---

**Last Updated:** 2026-04-02
**Version:** 1.0.0
