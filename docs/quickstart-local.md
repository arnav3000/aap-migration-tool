# Quick Start -- Local Install

Install and run AAP Migration Tool directly on your workstation.

## Prerequisites

- Python 3.12+
- Access to source and target AAP instances with admin API tokens

## Install

```bash
git clone https://github.com/arnav3000/aap-migration-tool.git
cd aap-migration-tool

# Option A: uv (fast)
uv venv --seed --python 3.12
source .venv/bin/activate
uv sync

# Option B: pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify:

```bash
aap-migrate --version
aap-migrate --help
```

## Configure

```bash
cp .env.example .env
```

Edit `.env` with your AAP connection details:

```bash
SOURCE__URL=https://aap24.example.com/api/v2
SOURCE__TOKEN=<your-source-token>
SOURCE__VERIFY_SSL=false
SOURCE__TIMEOUT=300

TARGET__URL=https://aap26.example.com/api/controller/v2
TARGET__TOKEN=<your-target-token>
TARGET__VERIFY_SSL=false
TARGET__TIMEOUT=300

MIGRATION_STATE_DB_PATH=sqlite:///./migration_state.db
```

> AAP 2.6 uses `/api/controller/v2` (Platform Gateway). AAP 2.4/2.5 uses `/api/v2`.

## Run a migration

### Interactive TUI (recommended for first-time users)

```bash
aap-migrate
```

### CLI commands

```bash
# Phased migration (follow dependency order)
aap-migrate migrate -r organizations --skip-prep
aap-migrate migrate -r credential_types --skip-prep
aap-migrate migrate -r credentials --skip-prep
aap-migrate migrate -r projects --skip-prep
aap-migrate migrate -r inventories --skip-prep
aap-migrate migrate -r hosts --skip-prep
aap-migrate migrate -r job_templates --skip-prep

# Validate
aap-migrate validate all --sample-size 4000
```

See the full dependency order in the [Architecture](architecture.md) doc or run `aap-migrate migrate --help`.

## Next steps

- [Containerized CLI](quickstart-container-cli.md) -- run migrations without installing Python
- [Web UI](quickstart-web.md) -- browser-based migration planner
