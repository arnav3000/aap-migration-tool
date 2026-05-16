# Quick Start -- Web UI

Run the full-stack web interface (React UI + FastAPI backend + PostgreSQL) using Podman Compose.

## Prerequisites

- Podman with `podman compose` (or Docker Compose)
- Access to source and target AAP instances

## Start the stack

```bash
git clone https://github.com/arnav3000/aap-migration-tool.git
cd aap-migration-tool/container

podman compose up -d --build
```

This starts three containers:

| Service | Port | Description |
|---------|------|-------------|
| `ui` | 9080 | Nginx serving the React app, proxying API requests |
| `engine` | 8000 | FastAPI backend |
| `db` | 5432 | PostgreSQL database |

Open <http://localhost:9080> in your browser.

## First-time setup

1. Go to **Settings** and add a connection for your source AAP instance (URL, token).
2. Add a connection for your target AAP instance.
3. Use **Test Connection** to verify both.

## Dependency analysis

1. Navigate to **Dependency Analysis**.
2. Select the source connection and click **Analyze**.
3. Review the organization dependency graph, migration phases, and quality scores.

## Migration planner

1. Go to **Migration Planner** and create a new plan.
2. Select one or more completed analysis scans.
3. The tool generates a phased migration order; drag organizations between phases to customize.
4. Save the plan and execute phases one at a time, tracking progress in **Jobs**.

## Quick migration

For simple resource-by-resource migration without a full plan, use the **Migrate** tab to preview and run individual resource types.

## Stopping

```bash
podman compose down
```

Add `-v` to also remove the database volume.

## Next steps

- [Architecture](architecture.md) -- detailed component overview
- [Local Install](quickstart-local.md) -- run the CLI without containers
