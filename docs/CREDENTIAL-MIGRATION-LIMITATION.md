# Credential Migration Limitation & Solutions

**CRITICAL ISSUE:** Credentials cannot be fully migrated from AAP 2.4 to AAP 2.6 due to AAP API encryption.

---

## The Problem

When exporting credentials via the AAP API, **all sensitive fields are returned as `$encrypted$`** instead of the actual values.

### Example: Container Registry Credential

**Source AAP 2.4 (actual values):**
```json
{
  "name": "Automation Hub Container Registry",
  "inputs": {
    "host": "192.168.100.26",
    "username": "admin",
    "password": "actual_password_here",  ← Real value stored
    "verify_ssl": true
  }
}
```

**Exported via API:**
```json
{
  "name": "Automation Hub Container Registry",
  "inputs": {
    "host": "192.168.100.26",
    "username": "admin",
    "password": "$encrypted$",  ← Cannot retrieve!
    "verify_ssl": true
  }
}
```

**Imported to Target AAP 2.6:**
```json
{
  "name": "Automation Hub Container Registry",
  "inputs": {
    "host": "192.168.100.26",
    "username": "admin",
    "password": "$encrypted$",  ← Useless credential!
    "verify_ssl": true
  }
}
```

---

## Affected Credential Types

| Credential Type | Encrypted Fields | Impact |
|----------------|------------------|---------|
| **SSH** | `password`, `ssh_key_data`, `ssh_key_unlock`, `become_password` | ❌ Cannot migrate |
| **Vault** | `vault_password`, `vault_id` | ❌ Cannot migrate |
| **Machine** | `password`, `become_password` | ❌ Cannot migrate |
| **Network** | `password`, `authorize_password`, `ssh_key_data` | ❌ Cannot migrate |
| **Source Control** | `password`, `ssh_key_data`, `ssh_key_unlock` | ❌ Cannot migrate |
| **Galaxy/Automation Hub** | `token`, `password` | ❌ Cannot migrate |
| **Container Registry** | `password`, `token` | ❌ Cannot migrate |
| **Cloud (AWS, Azure, GCP)** | Access keys, secrets, passwords | ❌ Cannot migrate |

---

## Your Current Environment

### Source AAP 2.4 - 8 Credentials

| ID | Name | Type | Status |
|----|------|------|--------|
| 1 | Demo Credential | SSH | ❌ Cannot migrate (password/key encrypted) |
| 8 | test_A | SSH | ⚠️ Uses "ASK" prompts (might work) |
| 2 | Ansible Galaxy | Galaxy API | ✅ No sensitive data (public) |
| 3 | Automation Hub Validated Repository | Galaxy API | ❌ Token encrypted |
| 4 | Automation Hub Published Repository | Galaxy API | ❌ Token encrypted |
| 5 | Automation Hub RH Certified Repository | Galaxy API | ❌ Token encrypted |
| 6 | Automation Hub Community Repository | Galaxy API | ❌ Token encrypted |
| 7 | Automation Hub Container Registry | Registry | ❌ Password encrypted |

**Result:** Only 1-2 credentials can migrate successfully. 6-7 will fail or be incomplete.

---

## Why This Happens

AAP encrypts credentials at rest for security. The API intentionally returns `$encrypted$` to prevent credential leakage. This is **by design** for security, but makes migration impossible.

From AAP API documentation:
> "Sensitive fields are returned as the string '$encrypted$' and cannot be retrieved via the API."

---

## Solution 1: HashiCorp Vault Integration (Recommended)

The AAP Bridge tool has built-in HashiCorp Vault support for this exact problem.

### How It Works:

1. **Before Migration:** Store all credentials in HashiCorp Vault
2. **During Migration:** Tool creates credential placeholders in target AAP
3. **After Migration:** Configure target AAP to retrieve secrets from Vault

### Setup Steps:

#### 1. Enable Vault in .env
```bash
# HashiCorp Vault Configuration
VAULT__URL=https://vault.example.com
VAULT__ROLE_ID=xxxxx
VAULT__SECRET_ID=xxxxx
VAULT__NAMESPACE=
VAULT__PATH_PREFIX=secret/aap
VAULT__TOKEN_TTL=3600
VAULT__VERIFY_SSL=true
```

#### 2. Pre-populate Vault with Credentials

```bash
# Store each credential in Vault before migration
vault kv put secret/aap/credentials/demo-credential \
  username=admin \
  ssh_key_data="$(cat ~/.ssh/id_rsa)" \
  ssh_key_unlock=passphrase

vault kv put secret/aap/credentials/automation-hub \
  token=your_actual_token_here \
  url=https://192.168.100.26/api/galaxy/
```

#### 3. Run Migration with Vault Lookup

```bash
aap-bridge migrate full --use-vault
```

#### 4. Configure Target AAP to Use Vault

```bash
# In target AAP, configure external credential source
# Settings → Credentials → External Credentials
# Point to HashiCorp Vault
```

### Advantages:
- ✅ Fully automated credential migration
- ✅ Credentials never exposed in plaintext
- ✅ Centralized secret management
- ✅ Auditable access logs
- ✅ Works for all credential types

### Disadvantages:
- ❌ Requires HashiCorp Vault deployment
- ❌ Additional infrastructure complexity
- ❌ Requires pre-populating Vault with credentials

---

## Solution 2: Manual Credential Recreation (Fallback)

If HashiCorp Vault is not available, credentials must be manually recreated.

### Steps:

#### 1. Export Credential Metadata
```bash
# Export credential structures (without secrets)
aap-bridge export -r credentials --output creds_export
```

#### 2. Document Each Credential
```bash
# Create credential inventory
cat creds_export/credentials/*.json | jq -r '.[] | "\(.name) - Type: \(.credential_type) - Owner: \(.organization)"' > credential_list.txt
```

#### 3. Manually Recreate in Target AAP

For each credential in the list:

**Via UI:**
1. Log in to target AAP 2.6
2. Go to: Resources → Credentials → Add
3. Enter name, type, organization
4. **Manually enter** passwords, keys, tokens
5. Save

**Via CLI (awx-cli):**
```bash
# Example: Recreate SSH credential
awx credentials create \
  --name "Demo Credential" \
  --credential_type 1 \
  --organization 1 \
  --inputs '{
    "username": "admin",
    "password": "actual_password_here",
    "ssh_key_data": "-----BEGIN RSA PRIVATE KEY-----\n..."
  }'
```

#### 4. Update Job Templates

After recreating credentials, update job templates to reference the new credential IDs:

```bash
# Update job template to use migrated credential
awx job_templates modify <template_id> \
  --credentials <new_credential_id>
```

### Advantages:
- ✅ No additional infrastructure needed
- ✅ Works in any environment
- ✅ Simple to understand

### Disadvantages:
- ❌ Time-consuming for many credentials
- ❌ Prone to human error
- ❌ No audit trail
- ❌ Credentials may be exposed during entry
- ❌ Requires original password/key access

---

## Solution 3: Database-Level Migration (Advanced/Unsupported)

**⚠️ WARNING: This approach is NOT supported by Red Hat and may violate support agreements.**

### Concept:
Directly migrate the encrypted credential data from the PostgreSQL database of source AAP to target AAP.

### Why This Is Dangerous:
- AAP 2.4 and 2.6 use different encryption keys
- Database schemas may differ between versions
- Could corrupt target AAP database
- Voids Red Hat support
- **NOT RECOMMENDED**

---

## Solution 4: AAP Backup/Restore (If Same Version)

If migrating between identical AAP versions (e.g., 2.4 → 2.4 on different hardware):

```bash
# On source AAP
awx-manage dumpdata > full_backup.json

# On target AAP (SAME VERSION)
awx-manage loaddata full_backup.json
```

**Limitations:**
- ❌ Only works for identical AAP versions
- ❌ Doesn't work for 2.4 → 2.6 migration
- ❌ All-or-nothing (can't selective migrate)

---

## Recommended Approach for Your Environment

Based on your 8 credentials:

### Option A: HashiCorp Vault (Ideal for Production)

If you have access to Vault:

1. Deploy HashiCorp Vault (or use existing instance)
2. Pre-populate Vault with all 8 credential secrets
3. Enable Vault integration in AAP Bridge .env
4. Run migration with `--use-vault` flag
5. Configure target AAP to use Vault as external credential source

**Estimated Time:** 2-4 hours (including Vault setup)

### Option B: Manual Recreation (Quickest for Testing)

If this is a test/dev environment:

1. Export credential metadata for reference
2. Manually create 8 credentials in target AAP UI
3. Copy/paste passwords, tokens, SSH keys from source
4. Update job templates to reference new credential IDs

**Estimated Time:** 30-60 minutes for 8 credentials

### Option C: Hybrid Approach

1. Migrate non-sensitive credentials automatically (Ansible Galaxy)
2. Manually recreate sensitive credentials (SSH, tokens)
3. Use Vault for most sensitive credentials (production keys)

---

## Testing Credential Migration

### Before Migration:
```bash
# Check credentials in source
curl -sk -H "Authorization: Bearer $SOURCE__TOKEN" \
  "https://localhost:8443/api/v2/credentials/" | jq '.count'
# Result: 8
```

### After Migration (with current limitation):
```bash
# Check credentials in target
curl -sk -H "Authorization: Bearer $TARGET__TOKEN" \
  "https://localhost:10443/api/controller/v2/credentials/" | jq '.count'
# Result: 0 (or incomplete credentials if attempted)
```

### After Manual Recreation:
```bash
# Verify all credentials exist
curl -sk -H "Authorization: Bearer $TARGET__TOKEN" \
  "https://localhost:10443/api/controller/v2/credentials/" | jq '.count'
# Result: 8 ✅
```

---

## Updating Migration Documentation

I'll update the main documentation to clearly state this limitation:

**Files to update:**
1. `README.md` - Add credential migration warning
2. `TESTING-24-TO-26.md` - Document credential recreation steps
3. `MIGRATION-TEST-RESULTS.md` - Add credential migration failure
4. `docs/LIMITATIONS.md` - Create comprehensive limitations doc

---

## Impact on Your Migration

### What Will NOT Migrate:
- ❌ 6-7 credentials with encrypted secrets (tokens, passwords, SSH keys)
- ❌ Any job templates that reference these credentials

### What WILL Migrate:
- ✅ Organizations (org_A, org_B)
- ✅ Users
- ✅ Teams
- ✅ Inventories
- ✅ Hosts (after fixing duplicates)
- ✅ Projects (if included)
- ✅ Job Templates (structure only, without working credentials)
- ✅ Workflows (structure only, without working credentials)

### Post-Migration Manual Work Required:
1. Recreate 6-7 credentials manually
2. Update job templates to reference new credential IDs
3. Test job template execution with new credentials

---

## Next Steps

**Immediate:**
1. Decide on credential migration approach (Vault vs Manual)
2. If manual: Begin documenting where to find original secrets
3. If Vault: Set up Vault instance and populate secrets

**Before Production Migration:**
1. Test credential recreation in dev environment
2. Document all credential mappings (source ID → target ID)
3. Create runbook for post-migration credential updates
4. Plan for job template credential re-linking

**During Migration:**
1. Complete resource migration (orgs, users, inventories, etc.)
2. Create credential placeholders or skip credentials entirely
3. Verify resource migration success

**After Migration:**
1. Manually recreate/configure all credentials
2. Update job templates with new credential references
3. Test job template execution
4. Verify automation workflows work end-to-end

---

## Frequently Asked Questions

### Q: Can I extract credentials from the AAP database directly?
**A:** Technically possible but **NOT recommended**. Credentials are encrypted with instance-specific keys. Direct extraction risks:
- Database corruption
- Security vulnerabilities
- Voiding Red Hat support
- Incompatibility between AAP versions

### Q: Does Red Hat have a solution for this?
**A:** Red Hat's official recommendation is:
1. Use HashiCorp Vault or CyberArk for credential management
2. Manually recreate credentials in target AAP
3. Use AAP's external credential plugins

### Q: Will future AAP versions fix this?
**A:** Unlikely. This is an intentional security design, not a bug. External credential management (Vault) is Red Hat's strategic direction.

### Q: Can I file a bug/feature request?
**A:** This is documented behavior, not a bug. However, you can request enhanced migration tools via Red Hat Support or Ansible community forums.

---

## Summary

| Method | Credentials Migrated | Effort | Recommended For |
|--------|---------------------|---------|-----------------|
| **HashiCorp Vault** | ✅ 100% | High (setup) | Production environments |
| **Manual Recreation** | ✅ 100% | Medium | Small # of credentials |
| **Database Direct** | ⚠️ Risky | High | **NOT RECOMMENDED** |
| **API Migration** | ❌ 0% | N/A | **NOT POSSIBLE** |

**For your 8 credentials:** Manual recreation is fastest for testing. Use Vault for production.
