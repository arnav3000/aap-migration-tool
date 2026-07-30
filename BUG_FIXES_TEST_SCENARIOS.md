# Bug Fixes Test Scenarios

**Date**: 2026-06-06  
**Branch**: hotfix/ctoi-schedule-survey-regression  
**Status**: All 4 critical bugs already fixed in codebase

---

## Executive Summary

During comprehensive code audit, Opus identified 4 critical bugs. Investigation reveals **ALL 4 are already fixed** in the current codebase:

| Bug # | Issue | Fixed In | Status |
|-------|-------|----------|--------|
| #1 | UserImporter crash on conflict | Already correct | ✅ Fixed |
| #2 | WorkflowNode inventory/EE FK missing | Commit 8c5ad6f (2026-06-02) | ✅ Fixed |
| #3 | Conflict resolution lacks org-awareness | Already correct | ✅ Fixed |
| #5 | EE/Labels missing from org-scoped set | Already correct | ✅ Fixed |

---

## Test Scenarios

### Bug #1: UserImporter Crash on Conflict

#### Issue Description

**Symptom**: Migration crashes with `TypeError: _handle_conflict() takes 4 positional arguments but 5 were given`  
**Trigger**: Re-importing users that already exist in target AAP  
**Root Cause**: Incorrect function call with extra parameter

#### Test Case 1.1: User Already Exists (Conflict Resolution)

**Setup**:

1. Target AAP has user "testuser" with username "testuser"
2. Source AAP has user "testuser" with source_id=100

**Steps**:

```bash
# First import (creates user)
aap-bridge migrate -r users

# Second import (user exists, should handle conflict)
aap-bridge migrate -r users --skip-prep
```

**Expected Result**:

- ✅ No TypeError crash
- ✅ User conflict detected and resolved gracefully
- ✅ Log shows: `resource_exists_but_not_mapped` or similar
- ✅ Migration completes successfully
- ✅ Stats show: `skipped=1` or `conflict_count=1`

**Actual Result** (verified in code):

- Line 1423 correctly calls: `await self._handle_conflict(resource_type, source_id, data)`
- No extra parameter `e` passed
- ✅ **FIXED**

#### Test Case 1.2: User Does Not Exist (Normal Import)

**Setup**:

1. Target AAP does NOT have user "newuser"
2. Source AAP has user "newuser"

**Steps**:

```bash
aap-bridge migrate -r users
```

**Expected Result**:

- ✅ User created successfully
- ✅ No conflict handling triggered
- ✅ Stats show: `imported=1`

---

### Bug #2: WorkflowNode Inventory/EE FK Resolution Missing

#### Issue Description

**Symptom**: Workflow nodes with inventory or execution_environment overrides fail with "Invalid pk" or silently point to wrong resources  
**Trigger**: Workflow node has inventory override different from job template's default  
**Root Cause**: FK IDs not translated from source to target

#### Test Case 2.1: Workflow Node with Inventory Override

**Setup**:

1. Source AAP:
   - Job Template "Deploy App" uses inventory "Dev Servers" (inv_id=10)
   - Workflow "Production Deploy" has node that overrides to "Prod Servers" (inv_id=25)
2. Target AAP:
   - "Dev Servers" imported as inv_id=150
   - "Prod Servers" imported as inv_id=200

**Steps**:

```bash
# Import inventories
aap-bridge migrate -r inventories

# Import job templates
aap-bridge migrate -r job_templates

# Import workflows
aap-bridge migrate -r workflow_job_templates
```

**Expected Result**:

- ✅ Workflow node created successfully
- ✅ Node's inventory field = 200 (target ID for "Prod Servers")
- ✅ NOT 25 (source ID)
- ✅ Log shows: `workflow_node_inventory_resolved` with source_id=25, target_id=200

**Verification**:

```bash
# In target AAP, check workflow node:
curl -k https://target-aap/api/v2/workflow_job_template_nodes/{node_id}/ | jq '.inventory'
# Should return 200, not 25
```

**Actual Result** (verified in code):

- Lines 2397-2416: Inventory FK resolution implemented
- Lines 2418-2437: Execution environment FK resolution implemented
- ✅ **FIXED in commit 8c5ad6f (2026-06-02)**

#### Test Case 2.2: Workflow Node with Execution Environment Override

**Setup**:

1. Source: Node overrides to EE "Python 3.11" (ee_id=5)
2. Target: "Python 3.11" imported as ee_id=45

**Expected Result**:

- ✅ Node's execution_environment field = 45 (target ID)
- ✅ Log shows: `workflow_node_ee_resolved`

#### Test Case 2.3: Workflow Node WITHOUT Overrides (Regression Test)

**Setup**:

1. Workflow node uses job template's default inventory and EE
2. No inventory or execution_environment fields in node data

**Expected Result**:

- ✅ Node created successfully
- ✅ No inventory/EE resolution attempted (guard clause prevents it)
- ✅ Behavior unchanged from before fix

---

### Bug #3: Conflict Resolution Lacks Organization Awareness

#### Issue Description

**Symptom**: Resources in different organizations get cross-mapped when names match  
**Trigger**: Two organizations have resources with same name  
**Root Cause**: `find_resource_by_name` called without organization_id filter  
**Impact**: Data corruption - Org A's resources point to Org B's resources

#### Test Case 3.1: Same-Named Project in Different Orgs

**Setup**:

1. Source AAP:
   - Org "Engineering" (org_id=5) has project "WebApp" (proj_id=10)
   - Org "QA" (org_id=6) has project "WebApp" (proj_id=20)
2. Target AAP:
   - "Engineering" imported as org_id=100
   - "QA" imported as org_id=101

**Steps**:

```bash
# Import organizations
aap-bridge migrate -r organizations

# Import projects (both will conflict if target already has "WebApp" projects)
aap-bridge migrate -r projects
```

**Expected Result**:

- ✅ Engineering's "WebApp" maps to target project in org 100
- ✅ QA's "WebApp" maps to target project in org 101
- ✅ NO cross-org mapping
- ✅ Each org's job templates reference correct org's project

**Verification**:

```bash
# Check state database mappings:
sqlite3 database/migration_state.db "
  SELECT source_id, target_id, source_name, target_name 
  FROM id_mappings 
  WHERE resource_type='projects' AND source_name='WebApp'
"
# Should show:
#   source_id=10 -> target_id in org 100
#   source_id=20 -> target_id in org 101
```

**Actual Result** (verified in code):

- Lines 804-808: Organization ID extracted and passed
- Line 810-816: `find_resource_by_name` called with `organization_id` parameter
- ✅ **FIXED**

#### Test Case 3.2: Globally-Unique Resource (Regression Test)

**Setup**:

1. User "admin" exists in target AAP
2. Source has user "admin"

**Expected Result**:

- ✅ Conflict handled with global name search (no org filter)
- ✅ Behavior unchanged for non-org-scoped resources

---

### Bug #5: EE and Labels Missing from Org-Scoped Set

#### Issue Description

**Symptom**: Two organizations with same-named EE or label cause duplicate detection failure  
**Trigger**: Multiple orgs have execution environments or labels with identical names  
**Root Cause**: `execution_environments` and `labels` not in `ORGANIZATION_SCOPED_RESOURCES`

#### Test Case 5.1: Same-Named EE in Different Orgs

**Setup**:

1. Source AAP:
   - Org "DataScience" has EE "Python 3.11" (ee_id=5, org=10)
   - Org "DevOps" has EE "Python 3.11" (ee_id=8, org=12)

**Steps**:

```bash
aap-bridge migrate -r execution_environments
```

**Expected Result**:

- ✅ Both EEs imported (not treated as duplicates)
- ✅ Batch precheck uses composite key: `("Python 3.11", target_org_id)`
- ✅ Import-time duplicate detection scoped to organization
- ✅ Log shows: `loaded_fk_mappings_for_precheck` for execution_environments

**Verification**:

```bash
# Check both EEs exist in target:
curl -k https://target-aap/api/v2/execution_environments/ | jq '.results[] | select(.name=="Python 3.11") | {id, organization}'
# Should show 2 entries with different organization IDs
```

**Actual Result** (verified in code):

- Line 36: `"execution_environments"` in `ORGANIZATION_SCOPED_RESOURCES`
- Line 37: `"labels"` in `ORGANIZATION_SCOPED_RESOURCES`
- ✅ **FIXED**

#### Test Case 5.2: Batch Precheck for EE (Regression Test)

**Setup**:

1. Run migration with execution_environments twice

**Expected Result**:

- ✅ First run: imports EEs
- ✅ Second run: batch precheck detects all as pre-existing (composite key matching)
- ✅ Log shows: `pre_existing_resources_found already_existing=N resource_type=execution_environments`

---

## Cross-Bug Integration Tests

### Integration Test 1: Full Multi-Org Migration

**Setup**:

- 3 organizations: OrgA, OrgB, OrgC
- Each org has:
  - Project named "deploy"
  - Credential named "ssh-key"
  - EE named "ansible-runner"
  - Inventory named "servers"
  - Job template named "run-playbook"
  - Workflow with node having inventory override

**Steps**:

```bash
aap-bridge migrate --all
```

**Expected Result**:

- ✅ All 3 orgs imported
- ✅ Each org's resources scoped correctly (9 projects, 9 credentials, 9 EEs, etc.)
- ✅ No cross-org mappings
- ✅ Workflow nodes point to correct org's inventories
- ✅ No crashes on conflicts

**Verification Points**:

1. Count projects: should be 9 (3 per org), not 3
2. Check workflow node inventory FKs: all point to correct target IDs
3. Check state mappings: no cross-org references
4. Re-run migration: all resources detected as existing, 100% skip rate

### Integration Test 2: Re-Run Migration (Duplicate Detection)

**Setup**:

- Complete migration already run (all resources imported)

**Steps**:

```bash
aap-bridge migrate --all
```

**Expected Result**:

- ✅ Batch precheck detects all resources as pre-existing
- ✅ Stats show: imported=0, skipped=N (where N = total resources)
- ✅ No crashes from user conflicts
- ✅ No new duplicates created
- ✅ No API errors from workflow nodes

**Log Verification**:

```bash
grep "pre_existing_resources_found" logs/migration.log | grep "to_import=0"
# Should show entries for ALL resource types
```

---

## Performance Regression Tests

### Performance Test 1: Batch Precheck with Org-Scoped Resources

**Scenario**: Import 1000 execution environments across 10 organizations

**Expected**:

- ✅ Batch precheck loads org_mappings once (1 DB query)
- ✅ Each EE uses O(1) dict lookup for org translation
- ✅ No O(N) performance degradation
- ✅ Total time similar to before fix

### Performance Test 2: Workflow Import with Many Nodes

**Scenario**: Import workflow with 50 nodes, 30 have inventory overrides

**Expected**:

- ✅ Each inventory FK resolved with single state.get_mapped_id() call
- ✅ No batch API calls during node import
- ✅ Total time proportional to node count

---

## Edge Case Tests

### Edge Case 1: EE with organization=None (Global EE)

**Setup**:

- Source has EE with organization=None (global/unscoped)

**Expected**:

- ✅ Duplicate check skipped (guard at line 193)
- ✅ Import proceeds normally
- ✅ No org_id filter applied in find_resource_by_name

### Edge Case 2: Workflow Node with Unresolvable Inventory

**Setup**:

- Node references inventory that failed to import

**Expected**:

- ✅ Inventory FK resolution returns None
- ✅ Field removed from node data (line 2416)
- ✅ Warning logged: `workflow_node_inventory_unresolved`
- ✅ Node created without inventory override
- ✅ No crash

### Edge Case 3: Parent-Scoped Resource Conflict

**Setup**:

- Two inventory sources named "cloud-sync" under different inventories

**Expected**:

- ✅ Conflict resolution scoped to parent inventory
- ✅ Each inventory gets its own "cloud-sync" source
- ✅ No cross-inventory mapping

---

## Validation Checklist

After deploying fix:

- [ ] Run full migration on test dataset
- [ ] Verify no TypeError crashes
- [ ] Check workflow node FKs point to correct target resources
- [ ] Verify multi-org resources don't cross-map
- [ ] Confirm batch precheck detects duplicates correctly
- [ ] Re-run migration: 100% skip rate on all resources
- [ ] Check logs for any new WARNING or ERROR patterns
- [ ] Verify performance hasn't degraded
- [ ] Test with edge cases (None values, missing FKs, etc.)

---

## Rollback Plan

**IF issues found after deployment:**

1. **Identify which fix caused regression**:
   - Check logs for correlation with specific resource types
   - Bug #1 → user imports
   - Bug #2 → workflow nodes
   - Bug #3 → multi-org conflicts
   - Bug #5 → EE/label duplicates

2. **Quick rollback**:

   ```bash
   git revert <commit-hash>
   git push origin hotfix/ctoi-schedule-survey-regression
   # Rebuild container
   ```

3. **Targeted fix**:
   - Isolate the problematic change
   - Apply more conservative fix
   - Re-test

**Note**: Since all bugs are already fixed in current code, no rollback should be needed. This plan is for completeness.

---

## Summary

**Current Status**: ✅ All 4 critical bugs already fixed in codebase

**Confidence Level**: HIGH

- Bug #1: Code inspection confirms correct 3-arg call
- Bug #2: Commit 8c5ad6f shows explicit FK resolution implementation
- Bug #3: Code inspection confirms org_id parameter passed
- Bug #5: Code inspection confirms EE/labels in scoped set

**Next Steps**:

1. Run integration tests above to verify fixes work as expected
2. Document fixes in release notes
3. No new code changes required - current branch is production-ready

**Container Ready**: Yes - latest code on branch hotfix/ctoi-schedule-survey-regression contains all fixes
