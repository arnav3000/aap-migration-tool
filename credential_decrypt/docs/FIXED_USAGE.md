# Fixed Credential Extraction - Resolved awx-manage shell Issues

## 🐛 Problem Identified

The original `run_extraction.sh` script had an issue with `awx-manage shell_plus`:

**Error:**
```
File "<console>", line 1
  encrypted_fields = []
  ^
SyntaxError
```

**Root Cause:**
- `awx-manage shell_plus` is an **interactive Python console**
- Piping a complex Python script to it causes it to execute **line-by-line**
- Multi-line Python constructs (functions, classes, etc.) fail in interactive mode
- The shell_plus auto-imports are helpful but don't work with piped scripts

---

## ✅ Solution: Multiple Execution Methods

I've created **fixed scripts** that try multiple execution methods:

### **New Files Created:**

```
credential_decrypt/
├── extract_credentials_standalone.py  ← NEW: Standalone Python script
└── run_extraction_fixed.sh            ← NEW: Fixed wrapper with fallback methods
```

---

## 🚀 Usage (Updated Instructions)

### **Step 1: Copy NEW Scripts to Controller**

```bash
# On your workstation
cd /Users/arbhati/project/git/aap-bridge-fork/credential_decrypt

# Copy the FIXED scripts
scp extract_credentials_standalone.py \
    run_extraction_fixed.sh \
    encrypt_credentials.sh \
    root@aap24-controller:/tmp/
```

### **Step 2: On Controller - Run Fixed Script**

```bash
# SSH to controller
ssh root@aap24-controller

# Navigate to /tmp
cd /tmp

# Make scripts executable
chmod +x run_extraction_fixed.sh encrypt_credentials.sh

# Run the FIXED extraction script
./run_extraction_fixed.sh
```

**What It Does:**

The script tries **3 different execution methods** automatically:

1. **Method 1:** Direct Python execution
   ```bash
   python3 extract_credentials_standalone.py
   ```

2. **Method 2:** Via `awx-manage shell` (non-interactive)
   ```bash
   cat extract_credentials_standalone.py | awx-manage shell
   ```

3. **Method 3:** Via `awx-python` (if available)
   ```bash
   awx-python extract_credentials_standalone.py
   ```

It will use **whichever method works** on your system!

---

## 📊 Expected Output

```
========================================
AAP 2.4 Credential Extraction (Fixed)
========================================

→ Extraction script: /tmp/extract_credentials_standalone.py
→ Output file: /tmp/credentials_decrypted.json

→ Trying execution methods...

Method 1: Direct Python execution
================================================================================
AAP 2.4 Credential Extraction & Decryption
================================================================================
Started: 2026-04-02T13:00:00.123456

Found 57 credentials

[1/57] SSH-Production-01 (ID: 1, Type: Machine)
  ✓ Decrypted: password
  ✓ Decrypted: ssh_key_data
  ✓ Decrypted: ssh_key_unlock
[2/57] Ansible-Vault-Prod (ID: 2, Type: Vault)
  ✓ Decrypted: vault_password
...
[57/57] GitHub-Token (ID: 89, Type: GitHub Personal Access Token)
  ✓ Decrypted: token

================================================================================
Summary
================================================================================
Total: 57
Processed: 57
Errors: 0
Completed: 2026-04-02T13:02:15.456789

✓ Method 1 succeeded!

✓ Extraction completed successfully using: direct-python

========================================
Results
========================================
  Total credentials: 57
  Successfully extracted: 57
  Errors: 0

Output files:
  JSON: /tmp/credentials_decrypted.json
  Logs: /tmp/extraction.log

First credential (sample):
{
  "id": 1,
  "name": "SSH-Production-01",
  "credential_type": "Machine",
  "organization": "IT Operations"
}

========================================
IMPORTANT - Next Steps:
========================================

1. ENCRYPT IMMEDIATELY (MANDATORY!):
   ./encrypt_credentials.sh gpg

2. DELETE UNENCRYPTED FILE:
   shred -u /tmp/credentials_decrypted.json

3. TRANSFER ENCRYPTED FILE:
   scp /tmp/credentials_decrypted.json.gpg user@workstation:/path/

⚠️  WARNING: File contains UNENCRYPTED SECRETS!
   Encrypt it NOW before doing anything else!
```

---

## 🔧 Technical Details

### **Why the Original Script Failed:**

```bash
# ORIGINAL (BROKEN):
awx-manage shell_plus < extract_all_credentials.py

# What happened:
# - shell_plus enters interactive mode
# - Tries to execute each line as a separate command
# - Multi-line Python code fails
```

### **How the Fixed Script Works:**

```bash
# METHOD 1 (Preferred):
python3 extract_credentials_standalone.py

# The script:
# 1. Initializes Django environment
# 2. Imports AWX models (Credential, CredentialType, etc.)
# 3. Imports decrypt_field utility
# 4. Runs extraction logic
# 5. Outputs JSON
```

**Key Fix:** The standalone script handles Django initialization itself:

```python
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'awx.settings.production')
django.setup()

# Now we can import AWX models
from awx.main.models import Credential
from awx.main.utils import decrypt_field
```

---

## ✅ Verification

After running, verify the output:

```bash
# Check file exists and has content
ls -lh /tmp/credentials_decrypted.json

# Validate JSON format
python3 -m json.tool /tmp/credentials_decrypted.json > /dev/null && echo "Valid JSON"

# Count credentials
python3 -c "import json; print('Credentials:', json.load(open('/tmp/credentials_decrypted.json'))['metadata']['processed_count'])"

# Check for real values (not $encrypted$)
python3 -c "import json; data=json.load(open('/tmp/credentials_decrypted.json')); print('First cred inputs:', list(data['credentials'][0]['inputs'].keys()))"
```

---

## 🆘 Troubleshooting

### **If All Methods Fail:**

Check the log file for details:
```bash
tail -50 /tmp/extraction.log
```

### **Common Issues:**

**1. Django Not Found:**
```
ModuleNotFoundError: No module named 'django'
```
**Fix:** Ensure you're running as the `awx` user or use `sudo su - awx`

**2. Settings Module Error:**
```
django.core.exceptions.ImproperlyConfigured: Requested setting ... but settings are not configured
```
**Fix:** The script should auto-detect this. If not, run:
```bash
awx-manage shell < extract_credentials_standalone.py
```

**3. Permission Denied:**
```
PermissionError: [Errno 13] Permission denied: '/tmp/credentials_decrypted.json'
```
**Fix:** Check write permissions in `/tmp/` or use a different directory

---

## 📋 Updated Quick Commands

```bash
# === ON WORKSTATION ===
cd credential_decrypt
scp extract_credentials_standalone.py run_extraction_fixed.sh encrypt_credentials.sh root@controller:/tmp/

# === ON CONTROLLER ===
ssh root@controller
cd /tmp
chmod +x run_extraction_fixed.sh encrypt_credentials.sh
./run_extraction_fixed.sh           # Extraction (auto-tries multiple methods)
./encrypt_credentials.sh gpg         # Encryption
shred -u /tmp/credentials_decrypted.json

# === TRANSFER ===
scp /tmp/credentials_decrypted.json.gpg user@workstation:/path/

# === CLEANUP CONTROLLER ===
shred -u /tmp/credentials_decrypted.json.gpg
rm -f /tmp/extraction.log
```

---

## 🎯 Benefits of Fixed Version

| Issue | Original | Fixed |
|-------|----------|-------|
| Execution method | Single (awx-manage shell_plus) | Multiple fallback methods |
| Interactive mode issues | ❌ Fails | ✅ Works |
| Django initialization | ❌ Assumed | ✅ Handled explicitly |
| Error handling | ❌ Basic | ✅ Comprehensive |
| Debugging | ❌ Difficult | ✅ Clear method reporting |

---

**Use the FIXED scripts for reliable extraction!** 🎉

**Last Updated:** 2026-04-02
