# What Is NOT Migrated - Complete Reference

**Last Updated**: 2026-03-24
**Migration Tool Version**: aap-bridge 0.1.0

This document provides a comprehensive list of all resources that are NOT automatically migrated by the aap-bridge tool, organized by reason.

---

## Table of Contents

1. [Not Implemented (No Importer)](#1-not-implemented-no-importer)
2. [Intentionally Excluded (By Design)](#2-intentionally-excluded-by-design)
3. [Read-Only Endpoints](#3-read-only-endpoints)
4. [Manual Migration Required](#4-manual-migration-required)
5. [Partial Migrations (Settings)](#5-partial-migrations-settings)
6. [Failed Imports (Dependency Issues)](#6-failed-imports-dependency-issues)

---

## 1. Not Implemented (No Importer)

These resource types are exported but do NOT have importers implemented yet.

### Workflow Job Templates
- **Count**: 2 resources
- **Status**: ❌ Importer not implemented
- **Reason**: Complex dependency resolution for workflow nodes
- **Workaround**: Recreate manually in target AAP
- **Future**: Importer planned for future release

### Credential Input Sources
- **Count**: 4 resources
- **Status**: ❌ Importer not implemented
- **Reason**: External credential source dependencies
- **Workaround**: Recreate manually after credentials are migrated
- **Future**: Low priority - uncommon resource type

---

## 2. Intentionally Excluded (By Design)

These resources are intentionally NOT migrated because they are runtime data, infrastructure, or auto-generated.

### A. Runtime/Historical Data (Never Migrated)

#### Jobs
- **Count**: 122 resources
- **Status**: ✅ Intentionally skipped
- **Reason**: Job execution history - not needed in new environment
- **Categories**:
  - `jobs` - Standard job execution records
  - `workflow_jobs` - Workflow execution records
  - `project_updates` - Project sync job logs
  - `inventory_updates` - Inventory sync job logs
  - `ad_hoc_commands` - Ad-hoc command execution records
  - `system_jobs` - System job execution records
  - `workflow_job_nodes` - Workflow execution node logs
- **Migration Strategy**: Start fresh in target AAP (job history not needed)

#### Notifications
- **Count**: Variable
- **Status**: ✅ Intentionally skipped
- **Reason**: Runtime notification instances (historical)
- **Migration Strategy**: Notification templates ARE migrated; instances regenerate on new jobs

#### Job Events
- **Count**: Thousands (not counted)
- **Status**: ✅ Intentionally skipped
- **Reason**: Individual job task results (too granular, historical)
- **Migration Strategy**: New jobs will generate new events

#### Activity Stream
- **Count**: Variable
- **Status**: ✅ Intentionally skipped
- **Reason**: Audit logs (historical data)
- **Migration Strategy**: Target AAP will create its own audit trail

### B. Infrastructure/Platform (Manual Setup)

#### Instances (Controller Nodes)
- **Count**: 1 resource
- **Status**: ✅ Intentionally skipped
- **Reason**: Infrastructure nodes - manual setup required
- **Migration Strategy**:
  - AAP 2.6 requires manual controller node configuration
  - Topology may differ (single node → HA cluster)
  - Documented in AAP installation guide

#### Instance Groups
- **Count**: 5 resources (migrated)
- **Status**: ⚠️ Migrated but verify manually
- **Reason**: Target environment may have different execution topology
- **Migration Strategy**:
  - Groups ARE migrated
  - Review and adjust for target infrastructure
  - May need to reassign to different physical instances

#### System Job Templates
- **Count**: 4 resources
- **Status**: ✅ Intentionally skipped
- **Reason**: Built-in AAP templates (auto-created by AAP)
- **Examples**:
  - Cleanup Activity Stream
  - Cleanup Expired OAuth 2 Tokens
  - Cleanup Expired Sessions
  - Gather Analytics
- **Migration Strategy**: AAP creates these automatically

### C. Metrics/Ephemeral Data

#### Host Metrics
- **Status**: ✅ Intentionally skipped
- **Reason**: Auto-generated, ephemeral data
- **Examples**:
  - `host_metric_summary_monthly` - Host usage metrics
- **Migration Strategy**: Target AAP will regenerate metrics

---

## 3. Read-Only Endpoints

These endpoints are read-only and cannot be imported.

| Endpoint | Description | Reason |
|----------|-------------|--------|
| `ping` | Health check endpoint | System-generated |
| `config` | System configuration | Read-only, managed by AAP |
| `dashboard` | Dashboard data | Auto-generated from other data |
| `metrics` | Prometheus metrics | Auto-generated metrics |
| `mesh_visualizer` | Visualization data | Auto-generated topology |
| `me` | Current user info | Context-specific |
| `unified_job_templates` | Meta-endpoint | Virtual aggregation |
| `unified_jobs` | Meta-endpoint | Virtual aggregation |

**Migration Strategy**: No action needed - these regenerate automatically

---

## 4. Manual Migration Required

These resources require manual configuration in the target environment.

### A. Authentication Tokens

#### OAuth Tokens
- **Resource Type**: `tokens`
- **Status**: ⚠️ Manual recreation required
- **Reason**: Short-lived, security-sensitive
- **Migration Strategy**:
  1. Applications ARE migrated (new client_id/client_secret generated)
  2. Users must re-authenticate to generate new tokens
  3. Update external systems with new OAuth credentials

### B. RBAC Roles

#### Role Assignments
- **Resource Type**: `roles`
- **Status**: ⚠️ Handled separately via RBAC import
- **Reason**: Complex permission model, team/organization dependencies
- **Migration Strategy**:
  1. Organizations, teams, users ARE migrated
  2. Role assignments recreated based on team memberships
  3. Manual verification recommended for custom roles

### C. Dynamic Inventory Sources

#### Inventory Sources
- **Resource Type**: `inventory_sources`
- **Status**: ⚠️ Manual recreation recommended
- **Reason**: External source dependencies (cloud providers, etc.)
- **Migration Strategy**:
  1. Static inventories ARE migrated
  2. Recreate dynamic inventory sources manually
  3. Configure cloud credentials for dynamic sources
  4. Test sync before using in jobs

---

## 5. Partial Migrations (Settings)

Settings are partially automated with manual review required for security.

### Auto-Imported Settings
- **Count**: 82 settings (29.1%)
- **Category**: Safe to copy
- **Examples**:
  - `ACTIVITY_STREAM_ENABLED`
  - `ORG_ADMINS_CAN_SEE_ALL_USERS`
  - `MANAGE_ORGANIZATION_AUTH`
  - `MAX_FORKS`
  - `STDOUT_MAX_BYTES_DISPLAY`
- **Status**: ✅ Automatically imported

### Manual Review Required (Environment-Specific)
- **Count**: 107 settings (37.9%)
- **Category**: URLs, paths, LDAP, hostnames
- **Examples**:
  - `AUTH_LDAP_SERVER_URI` - LDAP server URL
  - `AUTH_LDAP_1_GROUP_TYPE_PARAMS` - LDAP schema (version-specific)
  - `AUTOMATION_ANALYTICS_URL` - Red Hat Insights URL
  - `AWX_ISOLATION_BASE_PATH` - File system paths
  - `TOWER_URL_BASE` - AAP controller URL
- **Status**: ⚠️ Review SETTINGS-REVIEW-REPORT.md
- **Action**: Adjust values for target environment

### Manual Entry Required (Sensitive)
- **Count**: 93 settings (33.0%)
- **Category**: Passwords, tokens, secrets
- **Examples**:
  - `REDHAT_PASSWORD` - Red Hat customer portal password
  - `AUTH_LDAP_BIND_PASSWORD` - LDAP bind password
  - `SOCIAL_AUTH_GITHUB_SECRET` - GitHub OAuth secret
  - `SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET` - Google OAuth secret
- **Status**: ⚠️ Values redacted for security
- **Action**: Provide actual values manually via AAP UI or API

**Migration Workflow**:
1. ✅ Auto-import 82 safe settings (automated)
2. ⚠️ Review `SETTINGS-REVIEW-REPORT.md` for 107 environment-specific settings
3. ⚠️ Enter 93 sensitive values manually (passwords/tokens)

---

## 6. Failed Imports (Dependency Issues)

These resources were exported but failed to import due to dependency resolution issues.

### Current Failures (From Last Test)

| Resource Type | Total | Imported | Failed | Reason |
|---------------|-------|----------|--------|--------|
| **Schedules** | 15 | 4 | 11 | Job template dependencies |
| **Credentials** | 57 | 40 | 17 | Credential type dependencies |
| **Teams** | 16 | 9 | 7 | Organization dependencies |
| **Hosts** | 27 | 22 | 5 | Inventory dependencies |
| **Organizations** | 14 | 10 | 4 | Galaxy credential dependencies |

### Schedules (11 failed)
- **Reason**: Depends on job templates or workflow templates
- **Fix Strategy**:
  1. Ensure job templates are imported first
  2. For workflow schedules: wait for workflow importer
  3. Re-run import after dependencies resolved
  4. Or recreate manually

### Credentials (17 failed)
- **Reason**: Missing credential types or organization references
- **Fix Strategy**:
  1. Verify credential types imported (35/35 ✓)
  2. Check for custom credential types
  3. Verify organization mappings
  4. Re-import after fixing dependencies

### Teams (7 failed)
- **Reason**: Organization dependencies or role assignment issues
- **Fix Strategy**:
  1. Verify organizations imported (10/14)
  2. Check organization name mappings
  3. May need manual recreation for failed orgs

### Hosts (5 failed)
- **Reason**: Inventory dependencies or variable schema issues
- **Fix Strategy**:
  1. Verify inventories imported (13/13 ✓)
  2. Check host variables for compatibility
  3. Review error logs for specific issues

### Organizations (4 failed)
- **Reason**: Galaxy credential dependencies
- **Fix Strategy**:
  1. Recreate Automation Hub/Galaxy credentials
  2. Re-import organizations
  3. Or create manually and assign existing resources

---

## Migration Statistics Summary

### Total Resources Analysis

| Category | Count | Status |
|----------|-------|--------|
| **Source Resources** | 408 | Total in source AAP |
| **Successfully Migrated** | 231 | 56.6% |
| **Failed Imports** | 44 | 10.8% (fixable) |
| **Not Implemented** | 8 | 2.0% (workflow templates, credential input sources) |
| **Intentionally Skipped** | 125 | 30.6% (jobs, runtime data, infrastructure) |

### By Migration Strategy

| Strategy | Count | Examples |
|----------|-------|----------|
| **✅ Fully Automated** | 231 | Users, credential types, job templates, inventories |
| **⚠️ Partially Automated** | 82 | Settings (82 auto + 200 manual) |
| **⚠️ Manual Review** | 200 | Settings (107 env-specific + 93 sensitive) |
| **⚠️ Manual Recreation** | 50 | Workflow templates, inventory sources, tokens |
| **✅ Intentionally Skipped** | 125 | Jobs, runtime data, infrastructure |

---

## Recommendations by Resource Type

### Fully Automated (No Action Required)
✅ These migrate automatically with 100% success:
- Users
- Credential Types
- Execution Environments
- Inventories
- Inventory Groups
- Projects
- Job Templates
- Applications (with new OAuth credentials)

### Partially Automated (Review Required)
⚠️ These migrate but need verification:
- **Settings**: Review 200/283 settings manually
- **Instance Groups**: Verify topology matches target infrastructure
- **Applications**: Update external systems with new OAuth credentials

### Manual Recreation Required
❌ These must be recreated manually:
- **Workflow Job Templates** (2) - No importer yet
- **Inventory Sources** - Dynamic inventory connections
- **OAuth Tokens** - Users must re-authenticate
- **System Job Templates** - Auto-created by AAP

### Intentionally Skipped (No Migration Needed)
✅ These are not needed in target:
- **Jobs** (122) - Job execution history
- **Activity Stream** - Audit logs
- **Metrics** - Auto-generated data
- **Instances** (1) - Infrastructure nodes

---

## Getting Help

### For Failed Imports
1. Check `PROJECT-FAILURES-REPORT.md` for detailed analysis
2. Review logs in `logs/migration.log`
3. Check database: `sqlite3 migration_state.db`
4. Re-run import after fixing dependencies: `aap-bridge import --yes --phase phase3`

### For Settings
1. Review `SETTINGS-REVIEW-REPORT.md` (1824 lines)
2. Update environment-specific settings via AAP UI or API
3. Enter sensitive values manually (never copy passwords)

### For Manual Recreations
1. Use AAP 2.6 UI to recreate workflow templates
2. Configure dynamic inventory sources
3. Users re-authenticate for new OAuth tokens

---

## Future Enhancements

**Planned**:
- Workflow Job Template importer
- Credential Input Source importer
- Improved dependency resolution for schedules
- LDAP schema compatibility detection

**Under Consideration**:
- Selective job history migration (last N days)
- Activity stream export (for audit compliance)
- Automated RBAC role verification

---

**Reference Documents**:
- `LIVE-MIGRATION-TEST-REPORT.md` - Full migration test results
- `SETTINGS-REVIEW-REPORT.md` - Settings requiring manual review
- `PROJECT-FAILURES-REPORT.md` - Failed resource analysis
- `SETTINGS-MIGRATION-FIX-REPORT.md` - Settings bug fixes
