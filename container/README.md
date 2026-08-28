# AAP Bridge - Container Deployment Guide

Containerized deployment for running AAP Bridge as a temporary migration appliance.

## Quick Start

### Prerequisites

- Podman or Docker installed
- Access to source and target AAP instances
- Network connectivity to AAP instances

### Option 1: Using Podman/Docker Directly

```bash
# 1. Create working directory
mkdir -p ~/aap-migration/{database,logs,exports,xformed,config}
cd ~/aap-migration

# 2. Download container configuration
wget https://raw.githubusercontent.com/arnav3000/aap-bridge-fork/main/container/.env.container -O .env

# 3. Edit configuration with your credentials
vi .env
# Update:
#   SOURCE__URL=https://host.containers.internal:8443/api/v2
#   SOURCE__TOKEN="your-token"
#   TARGET__URL=https://host.containers.internal:10443/api/controller/v2
#   TARGET__TOKEN="your-token"

# 4. Build container
cd /path/to/aap-bridge-fork
podman build -f container/Containerfile -t aap-bridge:latest .

# 5. Run container
cd ~/aap-migration
podman run -d \
  --name aap-bridge \
  --network host \
  -v $(pwd)/database:/app/aap-bridge/database:Z \
  -v $(pwd)/logs:/app/aap-bridge/logs:Z \
  -v $(pwd)/exports:/app/aap-bridge/exports:Z \
  -v $(pwd)/xformed:/app/aap-bridge/xformed:Z \
  -v $(pwd)/.env:/app/aap-bridge/.env:Z \
  -v $(pwd)/config:/app/aap-bridge/config:Z \
  aap-bridge:latest

# 6. Enter container and run migration
podman exec -it aap-bridge bash
aap-bridge export -y && aap-bridge transform -y && aap-bridge import -y
```

### Option 2: Using Docker Compose / Podman Compose

```bash
# 1. Clone repository
git clone https://github.com/arnav3000/aap-bridge-fork.git
cd aap-bridge-fork/container

# 2. Create volumes directory
mkdir -p volumes/{database,logs,exports,xformed,config,auth}

# 3. Configure environment
cp .env.container .env
vi .env  # Update credentials and set AAP_TOKEN_ENCRYPTION_KEY

# 4. Start the full stack (PostgreSQL is the default database)
podman-compose up -d

# Optional: switch to SQLite by replacing MIGRATION_STATE_DB_PATH in .env,
# then bring the stack up again.

# 5. Run migration
podman exec -it aap-bridge bash
aap-bridge export -y && aap-bridge transform -y && aap-bridge import -y
```

### Default network exposure

- UI is published on `http://localhost:8080`
- API is reachable only through the UI reverse proxy at `/api/`
- WebSocket job logs are reachable only through the UI reverse proxy at `/ws/`
- PostgreSQL is internal to the compose network and is not published on a host port

## Containerized Test Workflow

The repository now includes a dedicated test image at `container/Containerfile.test`.
It installs the Python test stack, the frontend test stack, and nginx so the
default regression workflow can run entirely inside containers.

```bash
# Build the dedicated test image
make build-test

# Backend/API/CLI pytest suite
make c-test-backend

# Frontend tests plus production build
make c-test-frontend

# Runtime smoke checks for compose/nginx/startup script coverage
make c-test-smoke

# Full regression suite
make c-test-all
```

The default suite is self-contained. Live environment checks should stay behind
pytest markers such as `integration`, `requires_aap`, and `requires_vault` so
they remain opt-in.

## Architecture

### PostgreSQL Deployment (Default)

```
┌──────────────────┐         ┌──────────────────┐
│ ui (nginx)       │ ─────▶  │ engine (FastAPI) │
│ published :8080  │         │ internal only    │
└──────────────────┘         └────────┬─────────┘
                                      │
                                      ▼
                             ┌──────────────────┐
                             │ db (PostgreSQL)  │
                             │ internal only    │
                             └──────────────────┘
```

**Characteristics:**

- ✅ Default compose path
- ✅ Better performance for large migrations
- ✅ API and DB stay private behind the UI
- ✅ Data persists in the PostgreSQL volume

### SQLite Deployment (Alternate)

```
┌──────────────────┐         ┌──────────────────┐
│ ui (nginx)       │ ─────▶  │ engine (FastAPI) │
│ published :8080  │         │ internal only    │
└──────────────────┘         └────────┬─────────┘
                                      │
                                      ▼
                             ┌──────────────────┐
                             │ database/        │
                             │ migration_state  │
                             │ .db volume       │
                             └──────────────────┘
```

**Characteristics:**

- ✅ Supported fallback
- ✅ Simpler if you explicitly want a file-backed database
- ⚠️  Not the default compose path
- ⚠️  PostgreSQL container may still be present unless you customize your compose invocation

## Database Options

### Using PostgreSQL (Default)

**Configuration in `.env`:**

```bash
POSTGRES_PASSWORD=changeme
MIGRATION_STATE_DB_PATH=postgresql://aap_user:changeme@db:5432/aap_migration
```

**Important:** If you change `POSTGRES_PASSWORD`, update `MIGRATION_STATE_DB_PATH` to match.

### Using SQLite (Fallback)

Replace the database DSN in `.env`:

```bash
MIGRATION_STATE_DB_PATH=sqlite:///./database/migration_state.db
```

Then restart the stack:

```bash
podman-compose up -d
```

## Container Networking

### macOS / Podman VM

On macOS, Podman runs in a Linux VM. Use `host.containers.internal` to reach the host:

```bash
SOURCE__URL=https://host.containers.internal:8443/api/v2
TARGET__URL=https://host.containers.internal:10443/api/controller/v2
```

### Linux / Docker

On Linux with Docker, use `host.docker.internal` or `172.17.0.1` (Docker bridge IP):

```bash
SOURCE__URL=https://host.docker.internal:8443/api/v2
TARGET__URL=https://host.docker.internal:10443/api/controller/v2
```

Or use `--network host` and `localhost`:

```bash
SOURCE__URL=https://localhost:8443/api/v2
TARGET__URL=https://localhost:10443/api/controller/v2
```

## Optional Basic Auth

If the UI will be reachable by anyone beyond a single trusted admin host, enable nginx basic auth so the SPA, `/api/`, and `/ws/` are protected together.

1. Create an htpasswd file:

```bash
mkdir -p volumes/auth
htpasswd -c volumes/auth/.htpasswd admin
```

1. Enable auth in `.env`:

```bash
BASIC_AUTH_ENABLED=true
BASIC_AUTH_REALM="AAP Bridge"
```

1. Restart the UI container:

```bash
podman-compose up -d ui
```

## Persistent Data

All migration data persists on the host via volume mounts:

```
~/aap-migration/
├── database/
│   └── migration_state.db      # SQLite database (if using SQLite)
├── logs/
│   ├── migration.log           # Migration logs
│   └── credential-comparison.md # Reports
├── exports/
│   ├── organizations/          # Exported data from source
│   ├── projects/
│   └── ...
└── xformed/
    ├── organizations/          # Transformed data for target
    ├── projects/
    └── ...
```

## Building Custom Images

### Build from specific branch

```bash
podman build \
  -f container/Containerfile \
  --build-arg BRANCH=fix-containers \
  -t aap-bridge:fix-containers \
  .
```

### Build with custom Python version

```bash
podman build \
  -f container/Containerfile \
  --build-arg PYTHON_VERSION=3.11 \
  -t aap-bridge:py311 \
  .
```

## Troubleshooting

### Container can't reach AAP instances

**Problem:** `Failed to connect to localhost`

**Solution:** Use `host.containers.internal` instead of `localhost` in `.env`

### Database file not persisted

**Problem:** Database disappears when container restarts

**Solution:** Verify volume mount with `:Z` flag for SELinux:

```bash
-v $(pwd)/database:/app/aap-bridge/database:Z
```

### Permission denied errors

**Problem:** Container can't write to mounted volumes

**Solution:** Fix permissions:

```bash
chmod 777 database logs exports xformed auth
```

Or run container as root (not recommended for production):

```bash
podman run --user root ...
```

## Advanced Usage

### Running specific migration phases

```bash
podman exec -it aap-bridge bash

# Export only
aap-bridge export -y

# Transform only
aap-bridge transform -y

# Import only
aap-bridge import -y

# Granular import (step-by-step)
aap-bridge  # Launch TUI
```

### Viewing logs in real-time

```bash
# From host
tail -f ~/aap-migration/logs/migration.log

# From container
podman exec aap-bridge tail -f /app/aap-bridge/logs/migration.log
```

### Cleanup and start fresh

```bash
# Stop and remove container
podman stop aap-bridge
podman rm aap-bridge

# Clean migration data
rm -rf ~/aap-migration/{database,logs,exports,xformed}/*

# Recreate directories
mkdir -p ~/aap-migration/{database,logs,exports,xformed}

# Start new migration
podman run ...
```

## Security Best Practices

1. **Never commit `.env` with real credentials**
2. **Use read-only mounts** for config: `-v $(pwd)/config:/app/aap-bridge/config:ro`
3. **Run as non-root user** (default: appuser UID 1001)
4. **Set a unique `AAP_TOKEN_ENCRYPTION_KEY`** before starting the web stack
5. **Enable basic auth** when the UI is reachable by other users or shared networks
6. **Use secrets management** for production:

   ```bash
   podman secret create aap-source-token source-token.txt
   podman run --secret aap-source-token ...
   ```

## Support

- Issues: <https://github.com/arnav3000/aap-bridge-fork/issues>
- Documentation: <https://github.com/arnav3000/aap-bridge-fork/blob/main/README.md>
