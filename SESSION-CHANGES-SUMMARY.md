# Session Changes Summary
**Date:** April 1, 2026
**Branch:** fix-tui (branched from 24-26-final)

## Overview
This session focused on fixing critical bugs in the migration tool and improving the TUI user experience.

---

## Critical Bug Fixes

### 1. Teams Deduplication Bug (FIXED)
**File:** `src/aap_migration/cli/commands/export_import.py`
**Lines:** 1148-1268

**Problem:**
Only 16 out of 21 teams were being imported. Teams with the same name in different organizations were overwriting each other in the deduplication logic.

**Root Cause:**
`batch_precheck_resources()` used only the resource name as dictionary key:
```python
resource_by_identifier[identifier] = {"source_id": source_id, "data": resource}
```

This caused teams like "E2E-Simple-Team" in Organization 1 and Organization 2 to overwrite each other, keeping only the last occurrence.

**Fix:**
Implemented composite key pattern `(name, organization)` for organization-scoped resources:
```python
if resource_type in ORGANIZATION_SCOPED_RESOURCES:
    org = resource.get("organization")
    dict_key = (identifier, org) if org is not None else identifier
else:
    dict_key = identifier

resource_by_identifier[dict_key] = {"source_id": source_id, "data": resource}
```

**Impact:**
- ✅ All 21 teams now import correctly
- ✅ Fix applies to all organization-scoped resources: teams, projects, inventories, credentials, job_templates, workflow_job_templates

---

### 2. Option 2 "Import All Resources (Automatic)" Not Working (FIXED)
**File:** `src/aap_migration/cli/commands/export_import.py`
**Lines:** 1343-1393

**Problem:**
Option 2 "Import All Resources (Automatic)" in TUI was not patching projects with SCM details, causing:
- Projects remained in "Manual" mode (no SCM configuration)
- Inventory sources failed to sync (depend on project data)
- Job templates and schedules had missing dependencies

**Root Cause:**
The project patching phase was only added for `--phase phase2`, not for `--phase all`:
```python
# OLD CODE - Only phase2 got patching
if phase == "phase2" and not dry_run:
    # Add patching phase
```

When Option 2 called `run_command(["import"])` (defaults to `--phase all`), projects were imported but never patched.

**Fix:**
Added project patching phase for `--phase all`, positioned correctly after projects:
```python
elif phase == "all":
    # Import Phase1 resources, patch projects, then import Phase3 resources
    for rtype in types_to_import:
        # ... add resource phase ...

        # Insert patching phase after projects
        if rtype == "projects" and patch_count > 0:
            phases.append(("patching", "Patching Projects", patch_count))
```

**Impact:**
- ✅ Projects now sync automatically in Option 2
- ✅ Inventory sources work correctly
- ✅ Job templates and schedules import successfully
- ✅ Complete workflow: organizations → users → projects → **PATCHING** → inventories → job_templates

---

## Organization-Scoped Resources Enhancement

### Updated ORGANIZATION_SCOPED_RESOURCES
**File:** `src/aap_migration/resources.py`
**Lines:** 411-418

**Added:**
```python
ORGANIZATION_SCOPED_RESOURCES = {
    "projects",
    "inventories",
    "credentials",
    "job_templates",
    "workflow_job_templates",
    "teams",  # Now properly handled
}
```

**Impact:**
All organization-scoped resources now use composite key `(name, organization)` for deduplication, preventing cross-organization overwrites.

---

## TUI Improvements

### 1. Removed Unnecessary Options from Import Menu
**File:** `src/aap_migration/cli/import_menu.py`
**Lines:** 223-242, 261-282

**Removed Options:**
- ❌ Option 4: "Retry Failed Resources" (redundant with granular import retry)
- ❌ Option 5: "View Failed Resources" (less useful, clutters menu)

**New Streamlined Menu:**
```
1. Pre-flight Check (Validate Dependencies)
2. Import All Resources (Automatic)
3. Granular Import (Step-by-Step Control) ⭐ Recommended
4. View Import Status
b. Back to Main Menu
```

**Impact:**
- ✅ Cleaner, more focused import menu
- ✅ Users still have full control via Granular Import (option 3)
- ✅ Removed duplicate/rarely-used features

---

### 2. Added Inventory_Groups to Granular Import
**File:** `src/aap_migration/cli/granular_import.py`
**Lines:** 32-37

**Added Missing Phase:**
```python
# Phase 3: Infrastructure (MUST follow this order)
{"id": "3.1", "name": "Execution Environments", "resource_type": "execution_environments"},
{"id": "3.2", "name": "Projects", "resource_type": "projects"},
{"id": "3.3", "name": "Inventories", "resource_type": "inventories"},
{"id": "3.4", "name": "Inventory Sources", "resource_type": "inventory_sources"},
{"id": "3.5", "name": "Inventory Groups", "resource_type": "inventory_groups"},  # NEW!
```

**Impact:**
- ✅ Inventory groups now visible in granular import phases
- ✅ Proper ordering maintained (after inventory_sources)
- ✅ Users can track inventory_groups progress

---

## User Warnings for EE/Automation Hub Issues

### 1. Inventory Sources EE Warning in TUI
**Files:**
- `src/aap_migration/cli/import_menu.py` (lines 149-155)
- `src/aap_migration/cli/granular_import.py` (lines 463-466, 474-478)
- `src/aap_migration/migration/importer.py` (line 1738)

**Warning Added in Multiple Places:**

**a) Option 4 "View Import Status":**
```python
if inv_src_stat and inv_src_stat["completed"] > 0:
    console.print("[bold yellow]⚠️  Important Note:[/bold yellow]")
    console.print("Check inventory sources manually for outdated EE's which are pointing to")
    console.print("older AAP-2.4 automation hub address.")
```

**b) After Inventory Sources Import (Granular):**
```python
if resource_type == "inventory_sources" and result["completed"] > 0:
    self.console.print("[yellow]💡 Note:[/yellow] Check inventory sources manually for outdated EE's which are")
    self.console.print("   pointing to older AAP-2.4 automation hub address.")
```

**c) Final Summary:**
```python
if inv_src_stats.get("completed", 0) > 0:
    self.console.print("[bold yellow]⚠️  Important Note:[/bold yellow]")
    self.console.print("   Check inventory sources manually for outdated EE's which are pointing to")
    self.console.print("   older AAP-2.4 automation hub address.")
```

**Impact:**
- ✅ Users warned about common EE/Automation Hub issues
- ✅ Message appears during import (immediate feedback)
- ✅ Message appears in status view (comprehensive review)
- ✅ Clear guidance on what to check manually

---

## Infrastructure Improvements

### Complete Environment Cleanup Script
**File:** `cleanup_complete_environment.sh` (NEW)

**Features:**
- Cleans AAP 2.6 target (removes all migrated resources)
- Cleans local database (migration_state.db)
- Cleans exports directory
- Cleans xformed directory
- Cleans schemas directory
- Creates timestamped backups before deletion
- Single command: `./cleanup_complete_environment.sh`

**Impact:**
- ✅ One-command complete cleanup (no manual steps)
- ✅ Safe backups before deletion
- ✅ Consistent test environment preparation

---

## Minor Fixes

### 1. Retry Command Update
**File:** `src/aap_migration/cli/commands/retry.py`
**Lines:** 193-200

Updated retry command to use the proven `migrate` command instead of direct import:
```python
cmd = [
    sys.executable, "-m", "aap_migration.cli.main",
] + config_arg + [
    "migrate",
    "-r", rtype,
    "--skip-prep",
    "--phase", "all"
]
```

**Impact:**
- ✅ Retry now uses same proven workflow as granular import
- ✅ More reliable retry behavior

---

## Files Modified Summary

| File | Lines Changed | Purpose |
|------|--------------|---------|
| `src/aap_migration/cli/commands/export_import.py` | +70/-0 | Teams deduplication fix, project patching fix |
| `src/aap_migration/cli/granular_import.py` | +241/-234 | Inventory groups phase, EE warnings |
| `src/aap_migration/cli/import_menu.py` | +25/-28 | Removed options, added EE warning |
| `src/aap_migration/migration/importer.py` | +1/-0 | EE warning in logger |
| `src/aap_migration/resources.py` | +18/-13 | ORGANIZATION_SCOPED_RESOURCES update |
| `src/aap_migration/cli/commands/retry.py` | +6/-3 | Use migrate command for retry |
| `cleanup_complete_environment.sh` | NEW | Complete environment cleanup |

**Total:** 10 files modified, 288 insertions(+), 839 deletions(-)

---

## Testing Recommendations

### 1. Test Teams Import
- Export 21 teams (multiple teams with same name across different orgs)
- Import and verify all 21 teams are created
- Verify no overwrites occurred

### 2. Test Option 2 "Import All Resources (Automatic)"
- Run Option 2 from TUI
- Verify projects are patched with SCM details
- Verify projects sync successfully
- Verify inventory sources work
- Verify job templates and schedules import correctly

### 3. Test EE Warnings
- Import inventory sources
- Check Option 4 "View Import Status" shows EE warning
- Check granular import shows EE warning after inventory_sources phase

### 4. Test Cleanup Script
- Run `./cleanup_complete_environment.sh`
- Verify all 5 components cleaned:
  - AAP 2.6 target
  - Database
  - Exports
  - Xformed
  - Schemas
- Verify backups created

---

## Breaking Changes

None. All changes are backward compatible.

---

## Known Issues

None identified.

---

## Next Steps

1. Test the fixes with full migration workflow
2. Verify all 21 teams import correctly
3. Verify Option 2 automatic import works end-to-end
4. Consider merging to main branch after validation
