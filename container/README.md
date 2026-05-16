# AAP Bridge - Container Deployment Guide

Production-ready containerized deployment for AAP Bridge migration tool.

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
mkdir -p volumes/{database,logs,exports,xformed,config}

# 3. Configure environment
cp .env.container .env
vi .env  # Update with your credentials

# 4. Start with SQLite (default)
podman-compose up -d aap-bridge

# OR: Start with PostgreSQL
podman-compose --profile postgres up -d

# 5. Run migration
podman exec -it aap-bridge bash
aap-bridge export -y && aap-bridge transform -y && aap-bridge import -y
```

## Architecture

### SQLite Deployment (Default)

```
┌─────────────────────────────┐
│   aap-bridge container      │
│                             │
│  ┌─────────────────────┐    │
│  │ Python + SQLAlchemy │    │
│  │        ↓            │    │
│  │ database/           │←───┼─── Volume Mount
│  │  migration_state.db │    │
│  └─────────────────────┘    │
└─────────────────────────────┘
         ↓
    Host: ~/aap-migration/database/
          migration_state.db
```

**Characteristics:**
- ✅ Zero configuration
- ✅ No additional containers
- ✅ Suitable for most migrations
- ✅ File persists on host via volume

### PostgreSQL Deployment (Optional)

```
┌──────────────────┐         ┌──────────────────┐
│ aap-bridge       │         │ postgres         │
│ container        │◄────────┤ container        │
│                  │ Network │                  │
│ Python + psycopg2│         │ PostgreSQL 15    │
└──────────────────┘         │                  │
                             │ Data Volume      │
                             │   ↓              │
                             └────────┼─────────┘
                                      │
                                 postgres-data
                                    volume
```

**Characteristics:**
- ✅ Better performance for large migrations (100k+ resources)
- ✅ Concurrent access support
- ✅ Advanced querying capabilities
- ⚠️  Requires separate PostgreSQL container

## Database Options

### Using SQLite (Recommended for most users)

**Configuration in `.env`:**
```bash
MIGRATION_STATE_DB_PATH=sqlite:///./database/migration_state.db
```

**No additional setup required!** The database file is automatically created.

### Using PostgreSQL (For advanced users)

**1. Start PostgreSQL container:**
```bash
podman-compose --profile postgres up -d postgres
```

**2. Update `.env`:**
```bash
MIGRATION_STATE_DB_PATH=postgresql://aap_user:changeme@postgres:5432/aap_migration
```

**3. Verify connection:**
```bash
podman exec -it aap-bridge bash
python3 -c "from sqlalchemy import create_engine; engine = create_engine('postgresql://aap_user:changeme@postgres:5432/aap_migration'); print(engine.connect())"
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

### Build from specific branch:

```bash
podman build \
  -f container/Containerfile \
  --build-arg BRANCH=fix-containers \
  -t aap-bridge:fix-containers \
  .
```

### Build with custom Python version:

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
chmod 777 database logs exports xformed
```

Or run container as root (not recommended for production):
```bash
podman run --user root ...
```

## Advanced Usage

### Running specific migration phases:

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

### Viewing logs in real-time:

```bash
# From host
tail -f ~/aap-migration/logs/migration.log

# From container
podman exec aap-bridge tail -f /app/aap-bridge/logs/migration.log
```

### Cleanup and start fresh:

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
4. **Use secrets management** for production:
   ```bash
   podman secret create aap-source-token source-token.txt
   podman run --secret aap-source-token ...
   ```

## Support

- Issues: https://github.com/arnav3000/aap-bridge-fork/issues
- Documentation: https://github.com/arnav3000/aap-bridge-fork/blob/main/README.md
