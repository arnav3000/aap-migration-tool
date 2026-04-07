# Credential Extraction Toolkit for AAP 2.4 → AAP 2.6 Migration

## 🎯 Purpose

Extract and decrypt **ALL credentials** from AAP 2.4 controller to migrate them to AAP 2.6 with real secret values (not `$encrypted$` placeholders).

---

## 📂 Directory Structure

```
credential_decrypt/
├── README.md                           ← You are here
├── .gitignore                          ← Prevents committing secrets
├── credentials_decrypted.example.json  ← Example output format
│
├── scripts/                            ⭐ COPY THIS TO CONTROLLER
│   ├── README.md                       ← Scripts documentation
│   ├── extract_credentials_standalone.py
│   ├── run_extraction_fixed.sh
│   ├── encrypt_credentials.sh
│   ├── decrypt_credentials.sh          ← Keep on workstation
│   └── old/                            ← Deprecated scripts (don't use)
│
└── docs/                               📖 Documentation
    ├── README.md                       ← Complete guide
    ├── WORKFLOW.md                     ← Visual workflow with checklist
    └── FIXED_USAGE.md                  ← Technical details on fixes
```

---

## ⚡ Quick Start

### **Step 1: Copy Scripts to Controller**

```bash
# Copy entire scripts directory (recommended)
cd /path/to/aap-bridge-fork/credential_decrypt
scp -r scripts root@aap24-controller:/tmp/credential_scripts
```

**OR** copy individual files:

```bash
cd scripts
scp extract_credentials_standalone.py \
    run_extraction_fixed.sh \
    encrypt_credentials.sh \
    root@aap24-controller:/tmp/
```

---

### **Step 2: Run on Controller**

```bash
ssh root@aap24-controller
cd /tmp/credential_scripts  # or /tmp

chmod +x run_extraction_fixed.sh encrypt_credentials.sh
./run_extraction_fixed.sh              # Extract
./encrypt_credentials.sh gpg            # Encrypt (enter passphrase)
shred -u credentials_decrypted.json     # Delete unencrypted
```

---

### **Step 3: Transfer & Decrypt on Workstation**

```bash
# Transfer encrypted file
scp root@controller:/tmp/credential_scripts/credentials_decrypted.json.gpg ./scripts/

# Decrypt
cd scripts
./decrypt_credentials.sh gpg            # Enter same passphrase

# Use credentials_decrypted.json during migration
```

---

### **Step 4: Clean Up After Migration**

```bash
# On workstation
shred -u scripts/credentials_decrypted.json
shred -u scripts/credentials_decrypted.json.gpg

# On controller
ssh root@controller 'shred -u /tmp/credential_scripts/credentials_decrypted.json.gpg; rm -rf /tmp/credential_scripts'
```

---

## 🔐 Security Features

✅ **Mandatory encryption** before network transfer (GPG AES256)
✅ **Secure deletion** with shred (not just rm)
✅ **Restrictive permissions** (400 - read-only for owner)
✅ **No git commits** (.gitignore configured)
✅ **Multiple validation** steps (JSON, file format)
✅ **Minimal exposure** window for unencrypted data

---

## 📊 What You Get

**Output:** `credentials_decrypted.json` with:

```json
{
  "metadata": {
    "extraction_date": "2026-04-02T13:00:00",
    "total_credentials": 57,
    "processed_count": 57,
    "error_count": 0
  },
  "credentials": [
    {
      "id": 1,
      "name": "SSH-Production-01",
      "credential_type": "Machine",
      "organization": "IT Operations",
      "inputs": {
        "username": "ansible",
        "password": "RealPasswordHere",        ← Real value!
        "ssh_key_data": "-----BEGIN RSA...",   ← Real private key!
        "ssh_key_unlock": "RealPassphrase"     ← Real passphrase!
      }
    }
  ]
}
```

**All secret fields decrypted** - no `$encrypted$` placeholders!

---

## 🛠️ Supported Credential Types

✅ Machine (SSH)
✅ Source Control (Git, SVN)
✅ Vault
✅ Network
✅ Cloud (AWS, Azure, GCP)
✅ VMware vCenter
✅ OpenStack
✅ Red Hat Satellite 6
✅ **Custom Credential Types** (auto-detected)

---

## 📚 Documentation

| Document | Purpose |
|----------|---------|
| **[UPDATE_CREDENTIALS_GUIDE.md](UPDATE_CREDENTIALS_GUIDE.md)** | ⭐ How to update credentials after migration |
| **[scripts/README.md](scripts/README.md)** | Scripts usage and details |
| **[docs/README.md](docs/README.md)** | Complete step-by-step guide |
| **[docs/WORKFLOW.md](docs/WORKFLOW.md)** | Visual workflow with checklist |
| **[docs/FIXED_USAGE.md](docs/FIXED_USAGE.md)** | Technical fix details |

---

## 🆘 Troubleshooting

### **Extraction Fails**

Check log file:
```bash
tail -50 /tmp/credential_scripts/extraction.log
```

### **Can't Decrypt**

- Ensure you're using the **same passphrase** from encryption step
- Verify file not corrupted: `file credentials_decrypted.json.gpg`

### **Permission Denied**

Run as `awx` user or `root`:
```bash
sudo su - awx
cd /tmp/credential_scripts
./run_extraction_fixed.sh
```

---

## ⚠️ Critical Security Warnings

1. **Never transfer unencrypted files** over network
2. **Always use GPG encryption** (not base64)
3. **Shred files** after use (don't just rm)
4. **Don't commit to git** (already blocked by .gitignore)
5. **Delete immediately** after migration complete

---

## 🔄 Credential Update After Migration

After running the full migration, credentials will have `$encrypted$` placeholders. Use the update script to patch them with real values:

```bash
# After migration completes
cd credential_decrypt

# Test with dry-run first
python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json \
  --dry-run

# Apply updates
python3 update_credentials.py \
  --config ../config.yml \
  --credentials credentials_decrypted.json
```

See **[UPDATE_CREDENTIALS_GUIDE.md](UPDATE_CREDENTIALS_GUIDE.md)** for complete documentation.

---

## 🎯 Use Cases

### **Scenario 1: Standard Migration**
Export credentials from AAP 2.4 → Migrate to AAP 2.6 → Update with real values

### **Scenario 2: Credential Audit**
Extract all credentials to audit what secrets exist

### **Scenario 3: Backup/Disaster Recovery**
Create encrypted backup of all credential secrets

---

## 📞 Support

**Issues?** Check:
- [scripts/README.md](scripts/README.md) - Script usage
- [docs/README.md](docs/README.md) - Full documentation
- [docs/WORKFLOW.md](docs/WORKFLOW.md) - Step-by-step workflow

---

**Version:** 2.0 (Fixed)
**Last Updated:** 2026-04-02
**Status:** ✅ Production Ready
