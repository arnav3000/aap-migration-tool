# SCM-Based Inventories Created

## Overview

Successfully created inventories that dynamically source their hosts from Git projects, demonstrating AAP's ability to maintain dynamic inventory from source control.

---

## What Was Created

### 1. **Ansible Inventory Repository** (Project)
- **Type:** Git Project
- **Repository:** https://github.com/ansible/ansible-examples
- **Branch:** master
- **Purpose:** Contains inventory files that will be used as sources

---

### 2. **Dynamic SCM Inventory**
- **Type:** Inventory with SCM Source
- **Organization:** Engineering
- **Source Configuration:**
  - **Source Name:** Git Inventory Source
  - **Source Type:** SCM (Source Control Management)
  - **Source Project:** Ansible Inventory Repository
  - **Source Path:** `inventory`
  - **Update on Launch:** Yes
  - **Overwrite:** Yes
  - **Overwrite Variables:** Yes

**How it works:**
- Pulls inventory data from the `inventory` directory in the Git repository
- Automatically updates inventory before job execution (update_on_launch: true)
- Overwrites existing hosts and variables with fresh data from Git

---

### 3. **Project File Inventory**
- **Type:** Inventory with SCM File Source
- **Organization:** Engineering
- **Source Configuration:**
  - **Source Name:** Project Inventory File Source
  - **Source Type:** SCM
  - **Source Project:** Ansible Inventory Repository
  - **Source Path:** `hosts`
  - **Update on Launch:** Yes
  - **Overwrite:** Yes
  - **Overwrite Variables:** No (preserves manual variable additions)

**How it works:**
- Pulls inventory from a static `hosts` file in the Git repository
- Updates before each job launch
- Preserves manually added variables while updating host list

---

### 4. **Constructed Plugin Inventory** (Attempted)
- **Status:** Not supported in AAP 2.4
- **Note:** The constructed plugin source type is only available in AAP 2.5+
- This inventory was created but the source could not be added

---

## Key Features of SCM Inventories

### Dynamic Updates
- **Update on Launch:** Inventories automatically sync from Git before job execution
- **Manual Sync:** Can trigger sync via API or UI anytime
- **Cache Timeout:** Set to 0 for always-fresh data

### Version Control Benefits
- **Audit Trail:** All inventory changes tracked in Git
- **Collaboration:** Teams can propose inventory changes via pull requests
- **Rollback:** Easy to revert to previous inventory states
- **Testing:** Test inventory changes in branches before merging

### Supported Source Types in AAP 2.4
- **SCM:** Git/SVN repositories containing inventory files
- **EC2:** Amazon Web Services
- **GCE:** Google Compute Engine
- **Azure RM:** Microsoft Azure Resource Manager
- **VMware:** VMware vCenter
- **Satellite6:** Red Hat Satellite
- **OpenStack:** OpenStack clouds
- **RHV:** Red Hat Virtualization
- **Controller:** Other AAP/AWX instances
- **Insights:** Red Hat Insights

---

## Migration Testing Value

These SCM-based inventories are valuable for migration testing because they test:

1. **Inventory Source Migration:**
   - Source configurations (project, path, update settings)
   - Update schedules and cache timeouts
   - Overwrite settings

2. **Project Dependencies:**
   - Relationship between inventory sources and projects
   - Project credentials used by inventory sources

3. **Dynamic Updates:**
   - Update-on-launch functionality
   - Inventory sync jobs

4. **Complex Inventory Structures:**
   - Multi-source inventories
   - Different source types
   - Variable merging behavior

---

## How to Use

### View in AAP UI
1. Navigate to **Resources → Inventories**
2. Click on **Dynamic SCM Inventory** or **Project File Inventory**
3. Go to **Sources** tab to see the configured sources
4. Click **Sync** button to manually update from Git

### Use in Job Templates
Create job templates that use these inventories:
- The inventory will auto-update before each job run (if update_on_launch is enabled)
- Ensures jobs always run against current infrastructure state

### Update Inventory Data
1. Modify inventory files in the Git repository
2. Commit and push changes
3. Either:
   - Run a job (will auto-sync)
   - Or manually click Sync in AAP UI

---

## Current AAP 2.4 Environment

**Total Inventories:** 10
- 7 Static inventories (manually created hosts)
- 3 Dynamic inventories (SCM-sourced)
  - 2 functional (Dynamic SCM, Project File)
  - 1 placeholder (Constructed - not supported in 2.4)

**Total Projects:** 7
- 6 projects for job templates
- 1 project for inventory sources (Ansible Inventory Repository)

---

## Testing the Migration

When you run the migration, verify that:

1. **Inventory sources are migrated:**
   ```bash
   # Check source configurations
   aap-bridge validate all --sample-size 1000
   ```

2. **Source settings preserved:**
   - Source type (scm)
   - Source project reference
   - Source path
   - Update settings (update_on_launch, cache_timeout)
   - Overwrite settings

3. **Functionality intact:**
   - Sources can still sync successfully
   - Update-on-launch still works
   - Host data correctly pulled from Git

---

## Files Created

This setup created the following playbook:

- `playbooks/13_scm_inventory.yml` - Creates SCM-based inventories

To recreate:
```bash
export SOURCE__TOKEN="your_token"
ansible-playbook -i inventory.yml playbooks/13_scm_inventory.yml
```
