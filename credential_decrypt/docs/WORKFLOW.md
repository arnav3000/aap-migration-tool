# Secure Credential Extraction Workflow

## 📋 Complete Workflow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                     AAP 2.4 CONTROLLER                              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌───────────────────────────────────────┐
         │  Step 1: Extract Credentials          │
         │  ./run_extraction.sh                  │
         │                                        │
         │  Output: credentials_decrypted.json   │
         │  (UNENCRYPTED - Contains real secrets)│
         └───────────────────────────────────────┘
                              │
                              ▼
         ┌───────────────────────────────────────┐
         │  Step 2: Encrypt File (MANDATORY!)    │
         │  ./encrypt_credentials.sh gpg         │
         │                                        │
         │  Output: credentials_decrypted.json.gpg│
         │  (ENCRYPTED - Safe to transfer)       │
         └───────────────────────────────────────┘
                              │
                              ▼
         ┌───────────────────────────────────────┐
         │  Step 3: Delete Unencrypted           │
         │  shred -u credentials_decrypted.json  │
         │                                        │
         │  ⚠️  Only encrypted file remains       │
         └───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        NETWORK TRANSFER                             │
│  scp credentials_decrypted.json.gpg user@workstation:/path/        │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         WORKSTATION                                 │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌───────────────────────────────────────┐
         │  Step 4: Decrypt File                 │
         │  ./decrypt_credentials.sh gpg         │
         │                                        │
         │  Output: credentials_decrypted.json   │
         │  (UNENCRYPTED - Use immediately!)     │
         └───────────────────────────────────────┘
                              │
                              ▼
         ┌───────────────────────────────────────┐
         │  Step 5: Use During Migration         │
         │  - Reference for credential updates   │
         │  - Import to AAP 2.6                  │
         │  - Verify all credentials migrated    │
         └───────────────────────────────────────┘
                              │
                              ▼
         ┌───────────────────────────────────────┐
         │  Step 6: Secure Deletion              │
         │  shred -u credentials_decrypted.json  │
         │  shred -u credentials_decrypted.json.gpg│
         │                                        │
         │  ✓ All sensitive data destroyed       │
         └───────────────────────────────────────┘
```

---

## 🚦 Security Gates

### ✅ MUST DO

1. **Always encrypt before transfer** - Never send unencrypted credential files
2. **Use strong passphrase** - Minimum 16 characters, mix of chars
3. **Shred, don't delete** - Use `shred -u` instead of `rm`
4. **Limit access** - Only authorized personnel
5. **Delete immediately** - Remove after migration complete

### ❌ NEVER DO

1. **Email the file** - Even encrypted files shouldn't be emailed
2. **Commit to git** - Already blocked by .gitignore
3. **Store in cloud** - Unless it's encrypted filesystem
4. **Share passphrase** - Via insecure channels (Slack, Teams, etc.)
5. **Keep indefinitely** - Delete after use

---

## 🔢 Step-by-Step Checklist

### On AAP 2.4 Controller

- [ ] Copy scripts to /tmp
  ```bash
  scp *.py *.sh root@aap24-controller:/tmp/
  ```

- [ ] Run extraction
  ```bash
  ./run_extraction.sh
  ```

- [ ] Verify extraction succeeded
  ```bash
  jq '.metadata.processed_count' credentials_decrypted.json
  ```

- [ ] Encrypt file with GPG
  ```bash
  ./encrypt_credentials.sh gpg
  ```

- [ ] Enter and remember passphrase
  ```
  Passphrase: ____________________
  ```

- [ ] Verify encryption succeeded
  ```bash
  file credentials_decrypted.json.gpg  # Should show "PGP"
  ```

- [ ] Securely delete unencrypted original
  ```bash
  shred -u credentials_decrypted.json
  ```

### Transfer

- [ ] Copy encrypted file to workstation
  ```bash
  scp /tmp/credentials_decrypted.json.gpg user@workstation:/path/
  ```

- [ ] Verify file arrived
  ```bash
  ls -lh /path/credentials_decrypted.json.gpg
  ```

- [ ] Clean up controller
  ```bash
  shred -u /tmp/credentials_decrypted.json.gpg
  rm -f /tmp/extraction.log
  ```

### On Workstation

- [ ] Decrypt file
  ```bash
  ./decrypt_credentials.sh gpg
  ```

- [ ] Enter passphrase (same as encryption)

- [ ] Verify decryption succeeded
  ```bash
  jq '.metadata.total_credentials' credentials_decrypted.json
  ```

- [ ] Verify file permissions (should be 400)
  ```bash
  ls -l credentials_decrypted.json
  ```

### During Migration

- [ ] Reference credential data for imports
- [ ] Update credentials in AAP 2.6 with real values
- [ ] Verify all credentials work correctly

### After Migration

- [ ] Securely delete decrypted file
  ```bash
  shred -u credentials_decrypted.json
  ```

- [ ] Securely delete encrypted file
  ```bash
  shred -u credentials_decrypted.json.gpg
  ```

- [ ] Verify files deleted
  ```bash
  ls credentials_decrypted.json*  # Should show "No such file"
  ```

---

## 🎯 Success Criteria

✅ **Extraction Phase:**
- All credentials extracted with decrypted values
- No errors in extraction.log
- JSON validates with jq

✅ **Security Phase:**
- File encrypted with GPG/AES256
- Unencrypted original securely deleted
- Encrypted file only exists

✅ **Transfer Phase:**
- File transferred without corruption
- Controller cleaned (no traces)
- Workstation received encrypted file

✅ **Usage Phase:**
- File decrypted successfully
- All credentials data accessible
- Used during migration

✅ **Cleanup Phase:**
- Both files securely deleted (shred)
- No copies remaining
- Passphrase destroyed/forgotten

---

## ⏱️ Estimated Timeline

| Phase | Duration | Critical? |
|-------|----------|-----------|
| Extraction | 2-5 minutes | ✅ Yes |
| Encryption | 30 seconds | ✅ Yes |
| Transfer | 1-2 minutes | No |
| Decryption | 30 seconds | ✅ Yes |
| Migration Use | Variable | No |
| Cleanup | 1 minute | ✅ Yes |

**Total:** ~10-15 minutes (excluding migration time)

---

## 🔑 Passphrase Management

### Creating Strong Passphrase

```bash
# Option 1: Random strong passphrase (16 chars)
openssl rand -base64 16

# Option 2: Memorable but strong (4-5 random words)
# Example: "correct horse battery staple mountain"
```

### Storing Passphrase (Temporary)

```bash
# On paper (write down, destroy after migration)
Passphrase: _________________________________

# OR in password manager (KeePass, 1Password, etc.)
# Delete entry after migration complete
```

### ⚠️ Destroying Passphrase

After migration:
1. Delete from password manager
2. Shred paper note
3. Clear clipboard
4. Forget it (brain wipe 🧠)

---

## 🆘 Emergency Procedures

### If Unencrypted File Leaked

1. **Immediately rotate ALL credentials** in AAP 2.4 and 2.6
2. Inform security team
3. Audit who had access
4. Review logs for unauthorized access

### If Passphrase Forgotten

1. Re-run extraction from scratch
2. Create new encrypted file with new passphrase
3. Cannot decrypt old file (GPG is secure!)

### If Transfer Corrupted

```bash
# Verify file integrity
gpg --list-packets credentials_decrypted.json.gpg

# If corrupted, re-extract and re-encrypt from controller
```

---

## 📞 Support

**Questions?** Check:
- [Main README](./README.md)
- [Troubleshooting](./README.md#troubleshooting)
- [Security Considerations](./README.md#security-considerations)

**Last Updated:** 2026-04-02
