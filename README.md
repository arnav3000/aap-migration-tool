# AAP Bridge

A production-grade Python tool for migrating Ansible Automation Platform (AAP)
installations from one version to another, designed to handle large-scale
migrations

## Supported Versions

**Source AAP:**

- AAP 2.4 and 2.5 (RPM-based or containerized)

**Target AAP:**

- AAP 2.5 and 2.6 (containerized recommended)

**Common Migration Path:**

- AAP 2.4 (RPM-based) → AAP 2.6 (containerized)

The tool automatically detects AAP versions and validates compatibility before migration.

## Before You Begin

### Prerequisites: Running AAP Instances

This migration tool requires **two accessible AAP instances**:

- **Source AAP** (version 2.4 or 2.5)
- **Target AAP** (version 2.5 or 2.6)

#### Quick Health Check

Verify both AAP instances are running and accessible:

```bash
# Test Source AAP (should return version info)
curl -k https://your-source-aap/api/v2/ping/
# Expected: {"version": "2.4.x", ...}

# Test Target AAP (should return version info)
curl -k https://your-target-aap/api/controller/v2/ping/
# Expected: {"version": "2.6.x", ...}
```

✅ **If both commands return JSON with version info, you're ready to proceed.**

❌ **If you get connection errors:**

- Verify AAP instances are running
- Check network connectivity and firewall rules
- Verify URLs are correct (note `/api/controller/v2` for AAP 2.6)

#### Don't Have AAP Instances Yet?

**Option 1: Use Existing AAP Infrastructure**

- Contact your AAP administrator for access
- Use a source account with read access and a target account with write access;
  some resource types may need elevated privileges

**Option 2: Set Up Test Instances**

- Follow [AAP Installation Guide](https://access.redhat.com/documentation/en-us/red_hat_ansible_automation_platform)
- Size the environment for your AAP deployment and migration dataset
- Recommended ports: 8443 (source), 10443 (target)

**Option 3: Red Hat Demo Environment**

- Request AAP sandbox from Red Hat for testing

## Features

- **🎯 Interactive TUI**: User-friendly Text User Interface with guided workflows, real-time progress tracking, and step-by-step control
- **🔐 Credential-First Migration**: Checks credentials and imports them before resources that depend on them
- **Bulk Operations**: Leverages AAP bulk APIs for high-performance migrations
- **State Management**: SQLite or PostgreSQL-backed state tracking with checkpoint/resume capability
- **Idempotency**: Safely resume interrupted migrations without creating duplicates
- **Automatic Credential Comparison**: Pre-flight checks to identify missing credentials with detailed reports
- **Dynamic Inventories**: Full support for migrating dynamic inventories including:
  - Inventory containers
  - Inventory sources (SCM configuration)
  - Inventory source schedules
  - All hosts from dynamic inventories
- **Professional Progress Display**: Rich-based live progress display with
  real-time metrics (rate, success/fail counts, timing)
- **Flexible Output Modes**: Normal, quiet, CI/CD, and detailed modes for
  different environments
- **Comprehensive Logging**: Structured logging with separate console (WARNING)
  and file (DEBUG) levels
- **Split-File Export/Import**: Automatic file splitting for large datasets with
  metadata tracking
- **Dual Interface**: Interactive TUI for guided experience, or powerful CLI for automation and scripting
- **🖥️ Optional Web UI**: PatternFly console (second container, `--profile ui`) with AWX-style job output, connection management, and the full export → transform → import workflow in the browser
- **RBAC Migration**: Separate script for migrating role-based access control assignments

## Architecture

The tool is organized into several key components:

- **Client Layer**: HTTP clients for source AAP, target AAP, and HashiCorp Vault
  with retry logic and rate limiting
- **Migration Layer**: ETL pipeline with exporters, transformers, and importers
  for all AAP resource types
- **Credential Comparator**: Dedicated module for credential diff and validation
- **State Management**: Database-backed progress tracking, checkpoint creation,
  and ID mapping
- **CLI**: User-friendly command-line interface for all operations

## Quick Start

### Prerequisites

- **Python 3.12** or higher
- **Hardware**: Size capacity for your AAP deployment and migration dataset
- **Network**: Access to Source AAP and Target AAP
- **Permissions**: Read access on the source and write access on the target;
  elevated privileges may be needed for some resource types
- **Database**: SQLite by default or PostgreSQL for deployments that need a server-backed state database
- **HashiCorp Vault** (Optional but recommended): For migrating encrypted
  credentials securely

### Installation

#### Option 1: Containerized Deployment (Recommended) 🐳

Containerized deployment with no Python setup on the host:

```bash
# 1. Build the container
podman build -f container/Containerfile -t aap-bridge:latest .

# 2. Run the container with volume mounts
podman run -d --name aap-bridge --network host \
  -v $(pwd)/database:/app/aap-bridge/database:Z \
  -v $(pwd)/.env:/app/aap-bridge/.env:Z \
  aap-bridge:latest

# 3. Enter container and run migration
podman exec -it aap-bridge bash
aap-bridge --help
```

📖 **For detailed container setup and deployment options**, see the [Container Deployment Guide](https://github.com/arnav3000/aap-migration-tool/blob/main/container/README.md)

#### Option 2: Local Python Installation

```bash
# Clone the repository
git clone https://github.com/arnav3000/aap-migration-tool.git
cd aap-migration-tool

# Create virtual environment (using uv)
uv venv --seed --python 3.12
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
uv sync
```

**Alternative: Using standard Python venv**

If you don't have `uv` installed, use Python's built-in venv:

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package and dependencies
pip install -e .
```

**Verify Installation:**

```bash
# Check installation was successful
aap-bridge --version
# Should display: aap-bridge, version 1.0.0

# View available commands
aap-bridge --help

# Launch the interactive TUI (recommended for first-time users)
aap-bridge
```

💡 **Quick Tip:** First time using AAP Bridge? Launch the TUI with `aap-bridge` for a guided, interactive experience!

#### Option 3: Web UI (Optional) 🖥️

A PatternFly web console with an AWX-style job output view, served by an
optional second container. It talks only to the REST API — the CLI/TUI above
keep working exactly as before if you skip this.

**Option 3a: UI via containers (recommended)**

```bash
# 1. Configure environment (ONLY the API token — source/target AAP
#    endpoints are entered in the UI Setup page, never in this file)
cd container
cp .env.container .env
vi .env
# Set: AAP_BRIDGE_API_TOKEN=<a-long-random-token>

# 2. Start the API + UI (default CLI-only service is unchanged;
#    these two only start with the 'ui' profile)
mkdir -p volumes/{database,logs,exports,xformed,config,api_jobs}
podman-compose --profile ui up -d --build

# 3. Open the UI, enter your AAP_BRIDGE_API_TOKEN, then add your
#    source/target endpoints on the Setup page
# UI:  http://localhost:8080
# API docs: http://localhost:8000/api/v1/docs
```

**Option 3b: UI via local dev**

```bash
# 1. Start the API (from repo root, Python env from Option 2)
export AAP_BRIDGE_API_TOKEN=dev-token-change-me
aap-bridge-api
# Serves http://127.0.0.1:8000, docs at /api/v1/docs

# 2. Start the UI (Node.js 24+, new terminal)
cd ui
npm ci
npm run dev
# Open http://localhost:3000 (/api is proxied to :8000, no CORS setup needed)
```

📖 **For UI screens, architecture, and troubleshooting**, see the [UI Guide](ui/README.md)

### Configuration

The project includes configuration files with recommended default values. You need to set up your environment variables for AAP credentials and the database.

#### 1. Database Setup

The tool uses a database to track migration state (ID mappings, checkpoints, progress). **SQLite is the default** - no setup required!

**Database Comparison:**

| Feature | SQLite (Default) | PostgreSQL (Optional) |
|---------|------------------|----------------------|
| **Setup** | ✅ Zero configuration | Requires PostgreSQL server |
| **Capacity** | Large migrations | Very large migrations |
| **Location** | Local file | Local or remote |
| **Backup** | Copy single file | Database dump |
| **Best For** | Most migrations | Enterprise scale |

##### Option A: SQLite (Default - Zero Configuration) ⭐ Recommended

SQLite is a file-based database that requires no server setup. Perfect for most migrations.

- ✅ **No installation required** - Built into Python
- ✅ **Automatic setup** - Database file created on first run
- ✅ **Handles large migrations** - Supports substantial workloads
- ✅ **Easy backup** - Just copy the `migration_state.db` file

**No configuration needed!** The default `.env` uses SQLite.

##### Option B: PostgreSQL (Optional - For Enterprise Scale)

Consider PostgreSQL only if you need:

- Very large migrations
- Distributed/remote state access
- Cloud RDS integration

**Important:** This is a separate PostgreSQL instance for migration state tracking, NOT AAP's internal database.

```bash
# Create PostgreSQL database and user
psql -c "CREATE DATABASE aap_migration;"
psql -c "CREATE USER aap_migration_user WITH PASSWORD 'your_secure_password';"
psql -c "GRANT ALL PRIVILEGES ON DATABASE aap_migration TO aap_migration_user;"
# Ensure the user owns the schema/tables (Postgres 15+)
psql -d aap_migration -c "GRANT ALL ON SCHEMA public TO aap_migration_user;"
```

Then update `.env`:

```bash
MIGRATION_STATE_DB_PATH=postgresql://aap_migration_user:password@localhost:5432/aap_migration
```

#### 2. Getting AAP API Tokens

Use a source token with read access and a target token with write access.

##### Method 1: AAP Web UI (Recommended)

**For each AAP instance (repeat for Source and Target):**

1. Log in to AAP web interface
2. Click your **username** in the top-right corner
3. Select **"Tokens"** from the dropdown menu
4. Click **"Add"** or **"Create Token"**
5. Fill in the form:
   - **Description:** `Migration Tool - Source` (or `Target`)
   - **Application:** Leave blank
   - **Scope:** Use read scope for the source token and write scope for the target token
6. Click **"Save"**
7. **⚠️ Copy the token immediately!** It will only be shown once.

**Required Permissions:**

- The source account needs enough read access to enumerate the resources you
  intend to migrate. The target account needs write access to create them.
- Some resource types may require elevated privileges.
- Tokens don't expire by default but can be revoked

##### Method 2: CLI/API

```bash
# Generate token using username and password
curl -k -X POST https://your-aap/api/v2/tokens/ \
  -H "Content-Type: application/json" \
  -u "your_username:your_password" \
  -d '{
    "description": "Migration Tool",
    "scope": "write"
  }'

# Response includes your token - copy the "token" field value
```

##### Verify Your Tokens

Test that your tokens work before proceeding:

```bash
# Test Source token
curl -k -H "Authorization: Bearer YOUR_SOURCE_TOKEN" \
  https://your-source-aap/api/v2/me/
# Should return your user information

# Test Target token
curl -k -H "Authorization: Bearer YOUR_TARGET_TOKEN" \
  https://your-target-aap/api/controller/v2/me/
# Should return your user information
```

#### 3. Environment Configuration

Copy the example environment file and configure your credentials:

```bash

cp .env.example .env

```

Edit `.env` with your AAP instance details and database connection string.

##### Understanding AAP 2.6 Platform Gateway

⚠️ **Critical for AAP 2.6:** The API path is different!

```
AAP 2.4/2.5: https://your-aap/api/v2
AAP 2.6:     https://your-aap/api/controller/v2
             Note the /controller/ ^^^^^^^^^^^^
```

AAP 2.6 uses "Platform Gateway" which provides a unified API entry point. Always use `/api/controller/v2` for target AAP 2.6.

**How to verify:**

```bash
# AAP 2.6 responds to Platform Gateway path
curl -k https://your-aap26/api/controller/v2/ping/
```

##### Configuration Examples

**Example 1: Basic Setup (Recommended for Most Users)**

```bash
# .env file

# Source AAP 2.4 instance
SOURCE__URL=https://aap24-prod.company.com/api/v2
SOURCE__TOKEN=aBc123dEf456GhI789jKlMnO...  # Your actual token from step 2
SOURCE__VERIFY_SSL=false  # Use 'true' for production with valid SSL certs
SOURCE__TIMEOUT=300

# Target AAP 2.6 instance
TARGET__URL=https://aap26-prod.company.com/api/controller/v2  # Note: /api/controller/v2
TARGET__TOKEN=xYz987WvU654TsR321qPoNmL...  # Your actual token from step 2
TARGET__VERIFY_SSL=false  # Use 'true' for production with valid SSL certs
TARGET__TIMEOUT=300

# Database: SQLite (default - no setup needed!)
MIGRATION_STATE_DB_PATH=sqlite:///./migration_state.db

# Vault: Not using (credentials will need manual secret updates after migration)
# VAULT__URL=
# VAULT__ROLE_ID=
# VAULT__SECRET_ID=
```

**Example 2: With HashiCorp Vault (Optional)**

```bash
# Same as Example 1, plus:

# HashiCorp Vault configuration
VAULT__URL=https://vault.company.com:8200
VAULT__ROLE_ID=$VAULT_ROLE_ID
VAULT__SECRET_ID=$VAULT_SECRET_ID
VAULT__PATH_PREFIX=secret/aap/credentials
```

**Example 3: Enterprise Scale with PostgreSQL**

```bash
# Source and Target configs (same as Example 1)
SOURCE__URL=https://aap24-prod.company.com/api/v2
SOURCE__TOKEN=aBc123dEf456GhI789jKlMnO...
# ... (other SOURCE settings)

TARGET__URL=https://aap26-prod.company.com/api/controller/v2
TARGET__TOKEN=xYz987WvU654TsR321qPoNmL...
# ... (other TARGET settings)

# Database: PostgreSQL (for very large migrations)
MIGRATION_STATE_DB_PATH=postgresql://aap_migration_user:SecurePass123!@db-server.company.com:5432/aap_migration
```

**Common Configuration Values:**

| Setting | Recommended Value | Notes |
|---------|------------------|-------|
| `SOURCE__VERIFY_SSL` | `false` for testing, `true` for production | Set to `false` if using self-signed certs |
| `TARGET__VERIFY_SSL` | `false` for testing, `true` for production | Set to `false` if using self-signed certs |
| `SOURCE__TIMEOUT` | `300` | Increase for slow networks or large datasets |
| `TARGET__TIMEOUT` | `300` | Increase for slow networks or large datasets |

#### 4. Application Configuration

Review and adjust `config/config.yaml` for your environment:

- **Performance settings**: Adjust batch sizes and concurrency based on your AAP instance capacity
- **Logging**: Configure log levels and file paths
- **Migration phases**: Enable/disable specific resource types
- **Resource mappings**: Update `config/mappings.yaml` if you need to rename resources during migration (e.g., credential types with different names between AAP versions)

### Usage

#### Option 1: Interactive TUI (Recommended) 🎯

The easiest way to use AAP Bridge is through the interactive Text User Interface (TUI):

```bash
aap-bridge
```

**Why use the TUI?**

- ✅ User-friendly guided workflow
- ✅ Built-in progress tracking and status display
- ✅ Step-by-step control with granular import
- ✅ Real-time feedback and warnings
- ✅ Automatic dependency management
- ✅ Easy to pause, retry, or skip phases
- ✅ Visual progress bars and phase tracking

**Recommended TUI Workflow:**

1. Launch TUI: `aap-bridge`
2. Select **Option 1: Prep Phase** to discover endpoints and generate schemas.
3. Select **Option 2: Export (All)**.
4. Select **Option 3: Transform (All)**.
5. Select **Option 4: Import Resources (Enhanced)**. In its submenu, choose
   **Granular Import (Step-by-Step Control)** to import one phase at a time.
   - Import one phase at a time with full visibility
   - See exactly what's being imported in each phase
   - Check status after each phase completes

**Important Notes When Using TUI:**

- ⚠️ After importing inventory sources, check them manually for outdated Execution Environments pointing to older AAP-2.4 automation hub addresses
- ✓ Projects are automatically patched with SCM details during import
- ✓ All organization-scoped resources (teams, credentials, projects) are properly deduplicated

---

#### Option 2: Command Line Interface (CLI)

For automation, scripting, or advanced users, use the CLI commands directly:

```bash
# Step 1: Check what credentials are missing
aap-bridge credentials compare

# Step 2: Review the credential comparison report
cat ./reports/credential-comparison.md

# Step 3: Phased migration (following dependency graph to avoid failures)
# NOTE: Migrating one resource at a time provides better error isolation
# NOTE: If you encounter asyncio errors, use the manual three-step process shown below

# Phase 1: Foundation
aap-bridge migrate -r organizations --skip-prep
aap-bridge migrate -r users --skip-prep
aap-bridge migrate -r teams --skip-prep

# NOTE: Users and Teams Migration
# The above commands migrate ONLY local users and teams (created directly in AAP).
# For users authenticated via LDAP or Active Directory:
#   1. Migrate settings first (Phase 9 below) to configure LDAP/AD integration
#   2. SKIP the users/teams migration step - LDAP/AD users will authenticate automatically
#   3. Only migrate local admin/service accounts using the users command

# Phase 2: Credentials (review imports and re-enter required secrets)
aap-bridge migrate -r credential_types --skip-prep
aap-bridge migrate -r credentials --skip-prep

# Phase 3: Infrastructure
aap-bridge migrate -r execution_environments --skip-prep
aap-bridge migrate -r projects --skip-prep
aap-bridge migrate -r inventories --skip-prep
aap-bridge migrate -r inventory_sources --skip-prep

# Phase 4: Hosts
aap-bridge migrate -r hosts --skip-prep

# Phase 5: Instance Groups
aap-bridge migrate -r instance_groups --skip-prep

# Phase 6: Automation
aap-bridge migrate -r job_templates --skip-prep
aap-bridge migrate -r workflow_job_templates --skip-prep

# Phase 7: Applications (OAuth applications)
aap-bridge migrate -r applications --skip-prep

# Phase 8: Schedules
aap-bridge migrate -r schedules --skip-prep

# Phase 9: Settings (Optional - review before applying)
aap-bridge migrate -r settings --skip-prep

# Phase 10: Notification Templates (Optional - review before applying)
aap-bridge migrate -r notification_templates --skip-prep

# IMPORTANT: After settings migration, verify LDAP authentication
# In AAP 2.6, LDAP settings may be managed by Platform Gateway.
# Test LDAP login and manually enter AUTH_LDAP_BIND_PASSWORD if needed.
# See Post-Migration Verification section below for details.

# Step 4: Validate migration
aap-bridge validate

# Step 5: Migrate RBAC role assignments (after main migration)
python rbac_migration.py
```

**Understanding `--skip-prep`:**

The `--skip-prep` flag skips the schema discovery phase. Use it when:

- ✅ You've already run schema prep once (schemas already exist in `schemas/` directory)
- ✅ Running subsequent migration phases after Phase 1
- ✅ Re-running migrations in the same session

Don't use `--skip-prep` when:

- ❌ First time running the migration (no schemas exist yet)
- ❌ Schema files were deleted or need to be regenerated
- ❌ AAP instances were upgraded and schemas may have changed

**Note:** The `export`, `transform`, and `import` commands don't use `--skip-prep` because they don't perform schema discovery.

#### Post-migration: verify LDAP authentication (AAP 2.6)

For AAP 2.6, the settings importer sends supported LDAP authentication
configuration to Platform Gateway authenticators and maps. Other safe settings
are applied through the Controller API. The LDAP bind password is excluded
from migration and must be entered on the target.

After importing settings:

1. Test login with an LDAP user.
2. Verify the authenticator and maps in the AAP 2.6 UI under **Settings → Authentication**, or query the Gateway API:

   ```bash
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://target-aap/api/gateway/v1/authenticators/
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://target-aap/api/gateway/v1/authenticator_maps/
   ```

3. Enter the LDAP bind password in the target UI if required.
4. Review the generated settings report and complete any remaining follow-up actions.

#### Credential Management Commands

Dedicated credential management commands:

```bash
# Compare credentials between source and target
aap-bridge credentials compare [--output ./reports/creds.md]

# Migrate only credentials (and their dependencies)
aap-bridge credentials migrate [--dry-run] [--report-dir ./reports]

# Generate credential status report
aap-bridge credentials report [--output ./reports/status.md]
```

**What happens during credential migration:**

1. ✅ Compares credentials to find missing ones
2. ✅ Migrates organizations (dependency)
3. ✅ Migrates credential types (dependency)
4. ✅ Migrates credentials
5. ✅ Generates detailed migration report

#### Workaround: Manual Three-Step Migration

**⚠️ If `aap-bridge migrate -r <resource>` fails with asyncio or event loop errors, use the manual three-step process:**

The migration process consists of three separate phases that can be executed independently:

```bash
# Step 1: Export from source AAP
aap-bridge export -r organizations

# Step 2: Transform to target format
aap-bridge transform -r organizations

# Step 3: Import to target AAP
aap-bridge import -r organizations
```

**File locations:**

- Exported data: `exports/<resource_type>/`
- Transformed data: `xformed/<resource_type>/`
- State database: `migration_state.db`

---

#### NOTE: Re-Import Commands

**Re-importing after failures or manual deletions:**

```bash
# Retry failed imports (automatic - no special flag needed)
# Failed resources are automatically retried on re-run
aap-bridge import --yes

# Re-import specific resource type after manual deletion from target AAP
aap-bridge import -r hosts --force-reimport --yes
aap-bridge import -r inventories --force-reimport --yes
aap-bridge import -r job_templates --force-reimport --yes

# Re-import multiple resource types together
aap-bridge import -r workflow_job_templates -r workflow_nodes --force-reimport --yes

# Check what would be imported before running
aap-bridge import --check-dependencies
aap-bridge import --dry-run
```

**When to use which flag:**

| Scenario | Command | Flag |
|----------|---------|------|
| Retry after API/network failures | `aap-bridge import --yes` | None (auto-retry) |
| Re-import after manual deletion | `aap-bridge import -r <type> --force-reimport --yes` | `--force-reimport` |
| Check before importing | `aap-bridge import --check-dependencies` | `--check-dependencies` |
| Test without changes | `aap-bridge import --dry-run` | `--dry-run` |

**Important:**

- ✅ Failed resources (`status="failed"`) are **automatically retried** - no special flag needed
- ✅ `--force-reimport` clears import progress for specified types only (not dependencies)
- ✅ Use `--force-reimport` when resources were successfully imported but deleted from target AAP

---

#### Logging and progress

The top-level CLI accepts `--log-level` (`DEBUG`, `INFO`, `WARNING`, or
`ERROR`) and `--log-file`. These options must appear before the command:

```bash
aap-bridge --log-level INFO --log-file logs/migration.log migrate --skip-prep
```

`transform` also accepts `--quiet` and `--disable-progress`. The migration
configuration supports `LOGGING__DISABLE_PROGRESS=true` and
`LOGGING__SHOW_STATS=true` for progress display settings.

#### Split-File Export/Import

For large datasets, the tool automatically splits exports into multiple files:

```bash

# Export with custom split size (default: 1000 records/file)
aap-bridge export --output exports/ --records-per-file 500

# Import reads transformed data (not the raw exports)
aap-bridge import --input xformed/

```

**Export Structure:**

```text

exports/
├── metadata.json           # Export metadata
├── organizations/
│   └── organizations_0001.json
├── inventories/
│   ├── inventories_0001.json  # 1000 records
│   ├── inventories_0002.json  # 1000 records
│   └── inventories_0003.json  # Remaining records
└── hosts/
    ├── hosts_0001.json
    └── hosts_0002.json

```

## Key Design Principles

### Bulk Operations

The tool uses AAP's bulk operations API to dramatically improve performance:

- Hosts: 200 per request (API maximum)
- Inventories: 100 per batch
- Credentials: 50 per batch

### Checkpoint Recovery

All migrations are checkpoint-based, allowing safe resumption:

```bash

# Resume from last checkpoint
aap-bridge migrate resume

# Resume from a specific resource phase
aap-bridge migrate resume --from-phase inventories

```

### Idempotency

The tool tracks all migrated resources in a state database, ensuring that running the migration multiple times is safe and won't create duplicates.

## Migration Order and Dependencies

The migration registry defines resource ordering and importer dependencies.
For a standard run, use `aap-bridge migrate` to run the configured workflow, or
use `export`, `transform`, and `import` as separate steps. The import command
resolves dependencies automatically by default; `--check-dependencies` shows
the types it plans to import. Use `--skip-dependencies` only when you have
already handled prerequisites yourself.

Credential secrets are not returned by the AAP API. The tool migrates
credential metadata, but secret values may need to be entered again on the
target. Review the credential comparison and import reports before proceeding
to dependent resources.

## Known Issues and Limitations

### Important limitations

- **Credential secrets:** The AAP API masks secret fields (for example, with
  `$encrypted$`). The tool cannot recover those values; re-enter them on the
  target as needed.
- **AAP API paths:** Configure the source with its Controller API base URL
  (commonly `/api/v2`) and the target with the Platform Gateway path
  (`/api/controller/v2`) where applicable. The `.env.example` file shows the
  expected variable names.
- **RBAC:** `rbac_migration.py` is a separate script and is not part of the
  main ETL command sequence.
- **Environment-specific settings:** Review settings before importing them;
  source authentication and other environment-specific values may not be
  appropriate for the target.

### Dynamic inventories

The registry includes inventory sources, inventory groups, hosts, and host
membership resources. Source synchronization behavior depends on the resource
configuration and the target AAP instance; verify inventory data and sync
status in the target after import.

### Validation and development checks

Run the unit suite with `python -m pytest tests/unit/`. The repository also
provides `make lint`, `make typecheck`, and `make check` for development
checks. The checked-in test tree currently contains unit tests; integration
and performance test directories are not included.

## Supported resource types

The resource registry currently includes organizations, labels, users, teams,
credential types, credentials, credential input sources, execution
execution environments, instance groups, projects, inventories, inventory sources,
inventory groups, hosts, host inventory memberships, host group memberships,
instances, job templates, workflow job templates, system job templates,
schedules, notification templates, applications, and settings. The `jobs`
entry is an API-only type and is not exported or imported by the migration
workflow. Support and transformations vary by resource type; consult the
registry and review generated reports for the specific migration.

Credentials migrate as metadata because secret values are masked by the AAP
API. RBAC role assignments are handled separately by `rbac_migration.py`.

## Documentation

The maintained guides in this repository are:

- [Container deployment](container/README.md)
- [Optional web UI](ui/README.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

The environment and application configuration examples are in
[`.env.example`](.env.example), [`container/.env.container`](container/.env.container),
and [`config/config.yaml`](config/config.yaml).

### Getting Help

**Quick References:**

```bash
# Show all available commands
aap-bridge --help

# Show credential commands
aap-bridge credentials --help

# Show migration options
aap-bridge migrate --help

# View migration logs
tail -f logs/migration.log
```

**Common Questions:**

- How do I check which credentials are missing? → Run `aap-bridge credentials compare`
- Can I migrate only credentials? → Yes, run `aap-bridge credentials migrate`
- What if credentials fail to migrate? → Check `./reports/credential-comparison.md` and logs
- Why do secrets show as `$encrypted$`? → AAP API security - update secrets manually after migration
- Can I test without making changes? → Yes, use `--dry-run` flag

## Project Status

The package version declared in `pyproject.toml` is **1.0.0**. See
[CHANGELOG.md](CHANGELOG.md) for recorded changes.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

## Security

For security concerns and vulnerability reporting, please see [SECURITY.md](SECURITY.md).

## Support

- **Issues**: Report bugs and request features via [GitHub Issues](https://github.com/arnav3000/aap-migration-tool/issues)
- **Security**: Report vulnerabilities privately (see [SECURITY.md](SECURITY.md))

## Acknowledgments

Built following best practices from:

- Red Hat AAP documentation
- Red Hat Communities of Practice (COP) collections
- HashiCorp Vault integration patterns
- Python async/await patterns for high-performance API clients
