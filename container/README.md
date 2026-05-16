# Container Deployment

This directory contains everything needed to run AAP Migration Tool as containers.

## Files

| File | Purpose |
|------|---------|
| `Containerfile` | CLI-only image (headless migrations) |
| `Containerfile.api` | FastAPI backend + dependency analyzer |
| `Containerfile.ui` | Multi-stage build: React app compiled then served via Nginx |
| `docker-compose.yml` | Orchestrates PostgreSQL, API engine, and UI |
| `nginx.conf` | Reverse-proxies `/api` and `/ws` to the engine, serves static UI |

## Full-stack quick start (Web UI)

```bash
cd container
podman compose up -d --build
```

Services started:

| Service | Port | Description |
|---------|------|-------------|
| `db` | 5432 | PostgreSQL 15 (Red Hat UBI) |
| `engine` | 8000 | FastAPI backend |
| `ui` | 9080 | Nginx + React UI |

Open <http://localhost:9080>.

## CLI-only container

```bash
# Build
podman build -f container/Containerfile -t aap-migrate:latest .

# Run
podman run -d --name aap-migrate --network host \
  -v $(pwd)/database:/app/aap-bridge/database:Z \
  -v $(pwd)/.env:/app/aap-bridge/.env:Z \
  aap-migrate:latest

podman exec -it aap-migrate bash
aap-migrate --help
```

## Container networking

If your AAP instances run on the host:

| Platform | Host address |
|----------|-------------|
| macOS / Podman VM | `host.containers.internal` |
| Linux / Docker | `host.docker.internal` or `--network host` with `localhost` |

## Persistent data

Volume mounts keep all state on the host:

```text
database/   -- SQLite DB or PostgreSQL data
logs/       -- migration and API logs
exports/    -- exported source data
xformed/    -- transformed data ready for import
```

## Stopping

```bash
podman compose down       # keep data
podman compose down -v    # also remove volumes
```

## Troubleshooting

- **Container can't reach AAP**: use `host.containers.internal` instead of `localhost`.
- **Database not persisted**: verify the volume mount includes `:Z` for SELinux.
- **Permission denied**: run `chmod 777 database logs exports xformed` on the host directories.
