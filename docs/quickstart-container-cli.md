# Quick Start -- Containerized CLI

Run AAP Migration Tool in a container with no Python setup on the host.

## Prerequisites

- Podman (or Docker)
- Access to source and target AAP instances

## Build the image

```bash
git clone https://github.com/arnav3000/aap-migration-tool.git
cd aap-migration-tool

podman build -f container/Containerfile -t aap-migrate:latest .
```

## Prepare a working directory

```bash
mkdir -p ~/aap-migration/{database,logs,exports,xformed,config}
cp .env.example ~/aap-migration/.env
```

Edit `~/aap-migration/.env` with your AAP URLs and tokens.

> If your AAP instances are on the host machine, use `host.containers.internal` instead of `localhost` in the URLs.

## Run

```bash
cd ~/aap-migration

podman run -d \
  --name aap-migrate \
  --network host \
  -v $(pwd)/database:/app/aap-bridge/database:Z \
  -v $(pwd)/logs:/app/aap-bridge/logs:Z \
  -v $(pwd)/exports:/app/aap-bridge/exports:Z \
  -v $(pwd)/xformed:/app/aap-bridge/xformed:Z \
  -v $(pwd)/.env:/app/aap-bridge/.env:Z \
  -v $(pwd)/config:/app/aap-bridge/config:Z \
  aap-migrate:latest
```

Enter the container and run the migration:

```bash
podman exec -it aap-migrate bash

# Inside the container
aap-migrate --help
aap-migrate migrate -r organizations --skip-prep
# ... continue with remaining resource types
```

## Persistent data

All migration state, logs, and exported data are stored in the host-mounted volumes under `~/aap-migration/`.

## Cleanup

```bash
podman stop aap-migrate && podman rm aap-migrate
```

## Next steps

- [Web UI](quickstart-web.md) -- full browser-based migration workflow
- [Architecture](architecture.md) -- how the tool is structured
