# Quick Copy Guide

## 🎯 What to Copy to AAP 2.4 Controller

### **Option 1: Copy Entire Scripts Directory (Recommended)**

```bash
# From your workstation
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt

# Copy entire scripts directory
scp -r scripts root@aap24-controller:/tmp/credential_scripts
```

**What gets copied:**
```
scripts/
├── extract_credentials_standalone.py  ← Extraction script
├── run_extraction_fixed.sh            ← Wrapper
├── encrypt_credentials.sh             ← Encryption
├── decrypt_credentials.sh             ← (not needed on controller)
└── README.md                          ← Documentation
```

---

### **Option 2: Copy Only Required Files**

```bash
# Copy just the 3 essential files
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt/scripts

scp extract_credentials_standalone.py \
    run_extraction_fixed.sh \
    encrypt_credentials.sh \
    root@aap24-controller:/tmp/
```

---

## 📋 Full Workflow Commands

```bash
# === STEP 1: COPY TO CONTROLLER ===
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt
scp -r scripts root@aap24-controller:/tmp/credential_scripts

# === STEP 2: RUN ON CONTROLLER ===
ssh root@aap24-controller
cd /tmp/credential_scripts
chmod +x run_extraction_fixed.sh encrypt_credentials.sh

./run_extraction_fixed.sh              # Extract credentials
./encrypt_credentials.sh gpg            # Encrypt (enter passphrase!)
shred -u credentials_decrypted.json     # Delete unencrypted

# === STEP 3: TRANSFER TO WORKSTATION ===
exit  # Back to workstation
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt/scripts
scp root@aap24-controller:/tmp/credential_scripts/credentials_decrypted.json.gpg ./

# === STEP 4: CLEANUP CONTROLLER ===
ssh root@aap24-controller 'shred -u /tmp/credential_scripts/credentials_decrypted.json.gpg; rm -f /tmp/credential_scripts/extraction.log'

# === STEP 5: DECRYPT ON WORKSTATION ===
./decrypt_credentials.sh gpg            # Decrypt (enter passphrase!)

# === STEP 6: USE DURING MIGRATION ===
# credentials_decrypted.json now contains real secret values

# === STEP 7: CLEANUP AFTER MIGRATION ===
shred -u credentials_decrypted.json
shred -u credentials_decrypted.json.gpg
```

---

## 🔑 Important Notes

1. **Passphrase:** You'll enter it twice (encrypt on controller, decrypt on workstation)
2. **Keep scripts:** Don't delete `decrypt_credentials.sh` - you need it on workstation
3. **Security:** Never transfer `credentials_decrypted.json` without encrypting first!

---

## 📊 What Happens

```
Workstation                 Controller                  Workstation
    |                           |                           |
    ├─ scp scripts/ ──────────→ │                          │
    |                           ├─ run_extraction_fixed.sh │
    |                           ├─ encrypt_credentials.sh  │
    |                           │   (creates .gpg file)    │
    |                           │                          │
    │ ←──────── scp .gpg file ──┤                          │
    │                           │                          │
    ├─ decrypt_credentials.sh   │                          │
    ├─ USE credentials          │                          │
    └─ shred files              └─ shred files             │
```

---

## ✅ Verification Checklist

After copying, verify on controller:

```bash
ssh root@aap24-controller
ls -lh /tmp/credential_scripts/*.{py,sh}

# Should show:
# -rwxr-xr-x ... extract_credentials_standalone.py
# -rwxr-xr-x ... run_extraction_fixed.sh
# -rwxr-xr-x ... encrypt_credentials.sh
# -rwxr-xr-x ... decrypt_credentials.sh
```

---

**Ready to copy?** Just run the commands in STEP 1! 🚀
