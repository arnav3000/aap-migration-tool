# Organization-Specific Token Migration - Analysis & Design

**Status:** Planning Phase  
**Target Branch:** TBD (future work)  
**Date:** 2026-04-16  
**Author:** arnav3000

---

## Overview

This document analyzes the design and implementation approach for organization-specific token-based migrations. This allows customers to:

1. Create isolated org-admin users per organization
2. Generate org-specific API tokens
3. Migrate organizations independently with proper dependency ordering
4. Enable batch migrations with `--organization` flag

---

## Current State vs Desired State

**Current Workflow:**
```
Single global SOURCE_TOKEN → Migrates all orgs using admin credentials
Problem: Single point of failure, no org-level isolation
```

**Desired Workflow:**
```
Org1 → OrgAdmin1 → Token1 → Migrate Org1 (isolated)
Org2 → OrgAdmin2 → Token2 → Migrate Org2 (isolated)
Org3 → OrgAdmin3 → Token3 → Migrate Org3 (isolated)
```

**Customer Use Case:**
```bash
# Batch migration with dependency-aware ordering
$ for org in $(cat migration-order.txt); do 
    aap-bridge migrate --organization "$org"
done
```

---

## Part 1: Org Admin & Token Creation Tool

**Tool Name:** `aap-bridge create-org-tokens`

**Purpose:** Automate creation of org-specific admin users and tokens

**Workflow:**
```bash
# Input: List of organizations from dependency analyzer
$ aap-bridge create-org-tokens --from-analysis dependency-report.json

# Or manual list
$ aap-bridge create-org-tokens -o "Engineering" -o "Marketing" -o "QA-Team"

# Or all orgs
$ aap-bridge create-org-tokens --all
```

**What it does:**
1. Connects to SOURCE AAP using global admin token
2. For each organization:
   - Creates user: `aap-migration-admin-{org-name}` (e.g., `aap-migration-admin-Engineering`)
   - Assigns "Organization Admin" role to that user for ONLY that org
   - Generates API token for that user
   - Stores mapping in token file
3. Outputs: `org-tokens.json` file

**Token File Format Options:**

**Option A: Simple Key-Value**
```
Engineering=xGh7kP2...
Marketing=yTk9mN4...
QA-Team=zRl3vB8...
```
Pros: Simple, readable, bash-sourceable  
Cons: No metadata, hard to parse edge cases (org names with =)

**Option B: JSON (recommended)**
```json
{
  "source_url": "https://source-aap.example.com",
  "created_at": "2026-04-16T10:30:00Z",
  "organizations": {
    "Engineering": {
      "token": "xGh7kP2...",
      "username": "aap-migration-admin-Engineering",
      "org_id": 3,
      "created_at": "2026-04-16T10:30:05Z"
    },
    "Marketing": {
      "token": "yTk9mN4...",
      "username": "aap-migration-admin-Marketing",
      "org_id": 5,
      "created_at": "2026-04-16T10:30:08Z"
    }
  }
}
```
Pros: Structured, handles edge cases, stores metadata, can track which tokens are used  
Cons: More complex

**Option C: Hybrid (ENV-style with comments)**
```bash
# Generated: 2026-04-16T10:30:00Z
# Source: https://source-aap.example.com

# Engineering (org_id=3, user=aap-migration-admin-Engineering)
Engineering=xGh7kP2...

# Marketing (org_id=5, user=aap-migration-admin-Marketing)
Marketing=yTk9mN4...
```
Pros: Human-readable, bash-sourceable, has metadata  
Cons: Needs parsing for comments

**Recommendation:** JSON for flexibility and metadata tracking

---

## Part 2: New CLI Option `--organization`

**Command Signature:**
```bash
aap-bridge migrate --organization <org-name> [OPTIONS]
```

**Behavior:**

1. **Token Loading:**
   ```bash
   # Reads from org-tokens.json (default location: ./org-tokens.json)
   # Or custom location via --org-tokens-file
   
   $ aap-bridge migrate --organization Engineering
   
   # Internally:
   # 1. Load org-tokens.json
   # 2. Find "Engineering" -> get token
   # 3. Override SOURCE_TOKEN with org-specific token
   # 4. Run migration scoped to that org
   ```

2. **No Prompts Mode:**
   ```python
   # When --organization is used:
   # - Automatically answers "yes" to all prompts
   # - Skips confirmation dialogs
   # - Uses defaults for ambiguous choices
   # OR better: --non-interactive flag
   ```

3. **Configuration Precedence:**
   ```
   Priority (highest to lowest):
   1. CLI args: --organization Engineering
   2. Org tokens file: org-tokens.json
   3. Environment: SOURCE_TOKEN (fallback for backwards compat)
   4. Config file: config.yaml
   ```

**Integration Points:**

```bash
# Where does --organization fit in existing commands?

# Export (no change needed - uses global admin)
$ aap-bridge export

# Transform (already has -o filter - compatible!)
$ aap-bridge transform -o Engineering  # Already works

# Import (NEW - uses org-specific token)
$ aap-bridge import --organization Engineering --org-tokens-file ./org-tokens.json

# Or combined:
$ aap-bridge migrate --organization Engineering --org-tokens-file ./org-tokens.json
```

---

## Part 3: Dependency-Aware Batch Migration

**The Problem:**
```bash
# Customer tries:
$ for org in $(cat list-of-orgs); do 
    aap-bridge migrate --organization $org
done

# If list is:
# 1. E2E-Test-CustomEE-Org  ← Depends on Engineering
# 2. Engineering

# Migration FAILS because E2E-Test-CustomEE-Org migrates first!
```

**Solution 1: Manual Ordering (simple)**
```bash
# Customer uses dependency analyzer output to manually order list-of-orgs:
# 1. Engineering
# 2. E2E-Test-CustomEE-Org

# Then runs batch migration
$ for org in $(cat list-of-orgs); do 
    aap-bridge migrate --organization $org
done
```

**Solution 2: Auto-Ordering from Dependency Report (recommended)**
```bash
# Generate migration order file from dependency analysis
$ aap-bridge analyze-dependencies --all --format json --output deps.json

# Extract migration order
$ aap-bridge create-migration-order --from-analysis deps.json --output migration-order.txt

# migration-order.txt contains:
# Engineering
# Marketing
# E2E-Test-CustomEE-Org
# QA-Team

# Batch migrate in correct order
$ for org in $(cat migration-order.txt); do 
    aap-bridge migrate --organization $org --org-tokens-file org-tokens.json
done
```

**Solution 3: Built-in Batch Command (future enhancement)**
```bash
# Single command that handles everything
$ aap-bridge batch-migrate \
    --from-analysis deps.json \
    --org-tokens-file org-tokens.json \
    --parallel-phases  # Optional: run Phase 1 orgs in parallel

# Internally:
# 1. Load deps.json to get migration order
# 2. Load org-tokens.json for tokens
# 3. Migrate each org in dependency order
# 4. Stop on first failure (or --continue-on-error)
```

---

## Part 4: File Formats & Output

**From Dependency Analyzer to Migration:**

```json
// deps.json (output from analyze-dependencies --format json)
{
  "analysis_date": "2026-04-16T10:00:00Z",
  "source_url": "https://source-aap.example.com",
  "total_organizations": 14,
  "independent_orgs": ["Engineering", "Marketing"],
  "dependent_orgs": ["E2E-Test-CustomEE-Org", "QA-Team"],
  "migration_order": [
    "Engineering",
    "Marketing", 
    "E2E-Test-CustomEE-Org",
    "QA-Team"
  ],
  "migration_phases": [
    {
      "phase": 1,
      "description": "Independent organizations (can migrate in parallel)",
      "orgs": ["Engineering", "Marketing"]
    },
    {
      "phase": 2,
      "description": "Organizations depending on Phase 1",
      "orgs": ["E2E-Test-CustomEE-Org", "QA-Team"]
    }
  ],
  "organizations": {
    "Engineering": {
      "org_id": 3,
      "resource_count": 676,
      "has_dependencies": false,
      "can_migrate_standalone": true,
      "required_before": [],
      "dependencies": {}
    },
    "E2E-Test-CustomEE-Org": {
      "org_id": 8,
      "resource_count": 120,
      "has_dependencies": true,
      "can_migrate_standalone": false,
      "required_before": ["Engineering"],
      "dependencies": {
        "Engineering": [
          {
            "resource_type": "inventories",
            "resource_id": 8,
            "resource_name": "Demo Inventory",
            "used_by": [
              {"type": "workflow_job_templates", "id": 38, "name": "WF-1"},
              {"type": "workflow_job_templates", "id": 39, "name": "WF-2"}
            ]
          }
        ]
      }
    }
  }
}
```

This JSON can be used by:
1. **create-migration-order** - Extract just the `migration_order` array
2. **create-org-tokens** - Use `organizations` keys to know which orgs need tokens
3. **batch-migrate** - Use both `migration_order` and `organizations` for validation

---

## Part 5: Security Considerations

**Problem:** Tokens in plain text files

**Options:**

1. **Encrypt the token file:**
   ```bash
   # Create encrypted token file
   $ aap-bridge create-org-tokens --all --encrypt --password-file secret.key
   
   # Use with migration
   $ aap-bridge migrate --organization Engineering \
       --org-tokens-file org-tokens.json.enc \
       --decrypt-password-file secret.key
   ```

2. **Use secrets manager integration:**
   ```bash
   # Store tokens in vault/secrets manager
   $ aap-bridge create-org-tokens --all --store vault
   
   # Retrieve during migration
   $ aap-bridge migrate --organization Engineering --tokens-from vault
   ```

3. **Token expiration tracking:**
   ```json
   {
     "Engineering": {
       "token": "xGh7...",
       "created_at": "2026-04-16T10:30:00Z",
       "expires_at": "2026-04-23T10:30:00Z",
       "status": "valid"
     }
   }
   ```

4. **Cleanup after migration:**
   ```bash
   # Revoke all org-specific tokens after migration complete
   $ aap-bridge cleanup-org-tokens --org-tokens-file org-tokens.json
   
   # Or auto-revoke on success
   $ aap-bridge migrate --organization Engineering --revoke-token-on-success
   ```

---

## Part 6: Error Handling & Validation

**Pre-Migration Validation:**

```bash
$ aap-bridge migrate --organization E2E-Test-CustomEE-Org --validate-only

# Checks:
# ✓ Org exists in source AAP
# ✓ Token is valid and has org admin permissions
# ✓ Target AAP is reachable
# ✗ FAILED: Organization has dependencies that are not yet migrated:
#   - Engineering (required, not found in target)
#
# Recommendation: Migrate dependencies first:
#   1. aap-bridge migrate --organization Engineering
#   2. aap-bridge migrate --organization E2E-Test-CustomEE-Org
```

**Dependency Checking:**

```bash
# Option 1: Fail fast (default)
$ aap-bridge migrate --organization E2E-Test-CustomEE-Org
ERROR: Cannot migrate E2E-Test-CustomEE-Org - missing dependency: Engineering
Run with --ignore-dependencies to skip this check (not recommended)

# Option 2: Auto-migrate dependencies (dangerous!)
$ aap-bridge migrate --organization E2E-Test-CustomEE-Org --with-dependencies
Detected dependencies:
  - Engineering (required)
  
Migrating in order:
  1. Engineering... DONE
  2. E2E-Test-CustomEE-Org... DONE

# Option 3: Show migration plan
$ aap-bridge migrate --organization E2E-Test-CustomEE-Org --show-plan --no-execute
Migration Plan:
  Phase 1:
    - Engineering (676 resources)
  Phase 2:
    - E2E-Test-CustomEE-Org (120 resources)
    
Total: 796 resources across 2 organizations
Proceed? (y/n)
```

---

## Part 7: Complete Workflow Example

**End-to-End Workflow:**

```bash
# Step 1: Analyze dependencies
$ aap-bridge analyze-dependencies --all \
    --format json \
    --output dependency-analysis.json

# Step 2: Generate HTML report for review
$ aap-bridge analyze-dependencies --all \
    --format html \
    --output migration-plan.html

# (Customer reviews migration-plan.html, approves plan)

# Step 3: Create org-specific admin users and tokens
$ aap-bridge create-org-tokens \
    --from-analysis dependency-analysis.json \
    --output org-tokens.json

# Output:
# Created 14 organization admins:
#   ✓ Engineering: aap-migration-admin-Engineering (token: xGh7...)
#   ✓ Marketing: aap-migration-admin-Marketing (token: yTk9...)
#   ...
# Token mapping saved to: org-tokens.json

# Step 4: Generate migration order file
$ aap-bridge create-migration-order \
    --from-analysis dependency-analysis.json \
    --output migration-order.txt

# Output:
# Migration order (14 organizations, 3 phases):
# Phase 1 (5 orgs): Engineering, Marketing, ...
# Phase 2 (7 orgs): E2E-Test-CustomEE-Org, ...
# Phase 3 (2 orgs): Final-Integration-Org, ...

# Step 5: Batch migrate (automated)
$ for org in $(cat migration-order.txt); do
    echo "Migrating $org..."
    aap-bridge migrate \
        --organization "$org" \
        --org-tokens-file org-tokens.json \
        --non-interactive \
        --log-file "logs/migration-$org.log"
    
    if [ $? -ne 0 ]; then
        echo "FAILED: $org - check logs/migration-$org.log"
        exit 1
    fi
    echo "SUCCESS: $org"
done

# Step 6: Cleanup tokens (optional)
$ aap-bridge cleanup-org-tokens --org-tokens-file org-tokens.json
# Revoked 14 tokens
```

---

## Part 8: Command Additions Summary

**New Commands Needed:**

| Command | Purpose | Priority |
|---------|---------|----------|
| `create-org-tokens` | Create org admins & tokens, output mapping file | **HIGH** |
| `create-migration-order` | Extract migration order from dependency analysis | **MEDIUM** |
| `batch-migrate` | Automated batch migration with dependency handling | **LOW** (can use shell loop) |
| `cleanup-org-tokens` | Revoke org-specific tokens after migration | **MEDIUM** |

**Modified Commands:**

| Command | Change | Priority |
|---------|--------|----------|
| `migrate` | Add `--organization`, `--org-tokens-file`, `--non-interactive` | **HIGH** |
| `import` | Add `--organization`, `--org-tokens-file` | **HIGH** |
| `analyze-dependencies` | Add `--format json` output | **HIGH** ✅ (implemented) |

---

## Part 9: Configuration File Changes

**Current config.yaml:**
```yaml
source:
  url: https://source-aap.example.com
  token: ${SOURCE_TOKEN}
  verify_ssl: false

target:
  url: https://target-aap.example.com
  token: ${TARGET_TOKEN}
  verify_ssl: false
```

**Enhanced config.yaml:**
```yaml
source:
  url: https://source-aap.example.com
  token: ${SOURCE_TOKEN}  # Fallback - used if --organization not specified
  verify_ssl: false
  
  # Org-specific tokens (optional - can use external file)
  org_tokens_file: ./org-tokens.json
  
target:
  url: https://target-aap.example.com
  token: ${TARGET_TOKEN}
  verify_ssl: false

migration:
  # Behavior when --organization is used
  organization_mode:
    validate_dependencies: true  # Check if dependencies are migrated
    fail_on_missing_deps: true   # Stop if dependencies not found
    non_interactive: true         # No prompts when using --organization
    revoke_token_on_success: false  # Auto-cleanup tokens
```

---

## Part 10: Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Wrong migration order** | Dependencies fail, rollback needed | Use dependency analyzer, validate before migrate |
| **Token leakage** | Security breach | Encrypt token file, use secrets manager, auto-revoke |
| **Partial failure** | Some orgs migrated, some not | Transaction log, resume capability |
| **Org admin has insufficient permissions** | Migration fails mid-way | Validate permissions in create-org-tokens |
| **Token expires during batch migration** | Long migrations fail | Extend token TTL, refresh mechanism |
| **Network failure mid-batch** | Incomplete state | Idempotent migration, resume from last successful org |

---

## Implementation Recommendation

**Minimum Viable Implementation:**
1. ✅ `analyze-dependencies --format json` (generate dependency data) - **DONE in current branch**
2. ⏳ `create-org-tokens` (create org admins + token file)
3. ⏳ `migrate --organization <name> --org-tokens-file <file> --non-interactive`
4. ⏳ Manual shell loop for batching

**Future Enhancements:**
- `batch-migrate` command (automated)
- Token encryption/secrets manager
- Resume failed migrations
- Parallel migration within phases
- Web UI for monitoring batch progress

---

## Next Steps

1. Create new branch from `main`: `feature/org-token-migration`
2. Implement `create-org-tokens` command
3. Add `--organization` flag to `migrate` and `import` commands
4. Add org-tokens.json loading logic
5. Add dependency validation before migration
6. Test with multi-org environment

---

**Document Status:** Planning complete, ready for implementation
**Dependencies:** Requires `feature/dependency-analyzer` to be merged to main first
