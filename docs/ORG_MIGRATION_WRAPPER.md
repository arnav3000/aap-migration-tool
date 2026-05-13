# Organization Migration Wrapper

Wrapper script to simplify multi-organization migrations by managing organization-specific API tokens.

## Overview

When migrating multiple organizations with different API tokens, manually updating `.env` for each organization is tedious and error-prone. This wrapper automates the process.

## Setup

### 1. Create Token Mapping File

```bash
cp org.txt.example org.txt
```

Edit `org.txt` with your organization names and tokens:

```
# org.txt
prod-org=$PROD_ORG_TOKEN
staging-org=$STAGING_ORG_TOKEN
dev-org=$DEV_ORG_TOKEN
```

**Important:** `org.txt` is gitignored to prevent accidental token commits.

### 2. Prepare Environment File

Ensure you have a `.env` file with base configuration:

```bash
SOURCE_AAP_URL=https://source-aap.example.com
SOURCE_AAP_TOKEN=source_token_here

TARGET_AAP_URL=https://target-aap.example.com
# TARGET_AAP_TOKEN will be set by the wrapper script
```

## Usage

### Basic Syntax

```bash
./org-migration.sh --organization <org_name> [aap-bridge command]
```

### Examples

**Migrate organizations:**
```bash
./org-migration.sh --organization prod-org migrate -r organizations
```

**Full migration:**
```bash
./org-migration.sh --organization staging-org migrate --yes
```

**Generate report:**
```bash
./org-migration.sh --organization dev-org migration-report
```

**Short form:**
```bash
./org-migration.sh -o prod-org migrate -r projects
```

## How It Works

1. Reads organization name from `--organization` argument
2. Looks up the token in `org.txt`
3. Creates backup of `.env` (first run only)
4. Updates `TARGET_AAP_TOKEN` in `.env` using `sed`
5. Runs `aap-bridge` with remaining arguments

## Features

- **Token Lookup:** Automatically finds the correct token for the specified organization
- **Backup:** Creates `.env.org-backup` before first modification
- **Validation:** Checks if organization exists in `org.txt`
- **Clear Output:** Shows which organization and token are being used
- **Pass-through:** All additional arguments are passed to `aap-bridge`

## Example Workflow

```bash
# Setup
cp org.txt.example org.txt
vim org.txt  # Add your organizations and tokens

# Migrate Organization 1
./org-migration.sh --organization org1 migrate -r organizations --yes

# Migrate Organization 2
./org-migration.sh --organization org2 migrate -r organizations --yes

# Generate reports
./org-migration.sh --organization org1 migration-report -o logs/org1-report.md
./org-migration.sh --organization org2 migration-report -o logs/org2-report.md
```

## Restore Original .env

If you need to restore the original `.env`:

```bash
cp .env.org-backup .env
```

## Security Notes

- `org.txt` is automatically gitignored
- `.env.org-backup` is automatically gitignored
- Never commit `org.txt` or any file containing API tokens
- Use appropriate file permissions: `chmod 600 org.txt`

## Troubleshooting

**Error: Organization 'xyz' not found**
- Check spelling in `org.txt`
- Ensure no extra spaces around `=`
- Format must be: `org_name=token` (one per line)

**Error: aap-bridge not found**
- Activate virtual environment: `source .venv/bin/activate`
- Or install: `pip install -e .`

**Token not updating**
- Check that `.env` exists
- Verify `TARGET_AAP_TOKEN` line exists in `.env`
- Check permissions on `.env` file
