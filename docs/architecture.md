# Architecture

AAP Migration Tool is organized into layered components that can be used independently or together.

## High-level overview

```text
┌────────────────────────────────────────────────────────┐
│                      Interfaces                        │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │   CLI    │  │  FastAPI API  │  │   React Web UI   │ │
│  │ (Click)  │  │  (uvicorn)   │  │  (PatternFly v5) │ │
│  └────┬─────┘  └──────┬───────┘  └────────┬─────────┘ │
│       │               │                    │           │
│       └───────┬───────┘                    │           │
│               │  REST / WebSocket          │           │
│               ▼                            │           │
│  ┌─────────────────────────────────────────┘           │
│  │          Core Services                              │
│  │  ┌────────────────┐  ┌──────────────────────┐      │
│  │  │ Migration ETL  │  │ Dependency Analyzer  │      │
│  │  │ (export →      │  │ (graph analysis,     │      │
│  │  │  transform →   │  │  cycle detection,    │      │
│  │  │  import)       │  │  phase grouping)     │      │
│  │  └───────┬────────┘  └──────────┬───────────┘      │
│  │          │                      │                   │
│  │          ▼                      ▼                   │
│  │  ┌─────────────────────────────────────────┐       │
│  │  │           Client Layer                  │       │
│  │  │  httpx / awxkit with retry + rate-limit │       │
│  │  └──────────────────┬──────────────────────┘       │
│  │                     │                               │
│  │                     ▼                               │
│  │  ┌──────────────────────────────────────┐          │
│  │  │        State / Persistence           │          │
│  │  │  SQLAlchemy (SQLite or PostgreSQL)   │          │
│  │  │  ID mappings, checkpoints, jobs      │          │
│  │  └──────────────────────────────────────┘          │
│  └─────────────────────────────────────────────────────│
└────────────────────────────────────────────────────────┘
```

## Components

### CLI (Click)

The original interface. Provides commands for `migrate`, `export`, `transform`, `import`, `validate`, `cleanup`, and `credentials`. Also includes a menu-driven TUI mode launched by running `aap-migrate` with no arguments.

### FastAPI API

A REST API layer (`src/aap_migration/api/`) that exposes the same functionality the CLI uses but over HTTP. Key routers:

| Router | Purpose |
|--------|---------|
| `connections` | CRUD for saved AAP connections (tokens encrypted at rest) |
| `analysis` | Run dependency analysis and retrieve reports |
| `migration` | Preview, execute, and track migrations |
| `planner` | Multi-source migration plans with phase editing |
| `objects` | Browse objects on a connected AAP instance |
| `jobs` | List and inspect background jobs and logs |
| `cleanup` | Delete migrated resources from the target |

Background work (analysis scans, migrations, cleanup) runs as `asyncio` tasks managed by `JobService`, which persists job state, structured events, and log lines to the database so they survive restarts.

### React Web UI

A single-page app in `web/` built with Vite, React 18, and PatternFly v5. Communicates with the FastAPI backend. Major pages:

- **Jobs** -- live and historical job list with log streaming via WebSocket.
- **Migration Planner** -- select completed dependency-analysis scans, review/edit phased migration plans, execute phases.
- **Dependency Analysis** -- trigger scans, view org-level dependency graphs, migration phases, and quality scores.
- **Migrate** -- quick single-resource migration (export/transform/import).
- **Object Browser** -- inspect objects on a connected AAP instance.
- **Cleanup** -- remove migrated resources from the target with a confirmation gate.
- **Settings** -- manage AAP connections.

### Dependency Analyzer

Located in `src/aap_migration/analysis/`. Connects to a source AAP instance, builds a directed graph of inter-organization dependencies, detects circular dependencies, and groups organizations into migration phases. The analysis report includes quality scores, shared resources, and a recommended migration order.

### Client Layer

`src/aap_migration/client/` provides `AAPSourceClient` and `AAPTargetClient` (both extending `BaseAAPClient`) with:

- Automatic pagination
- Configurable retry with exponential backoff (via `tenacity`)
- AAP version detection (2.4 / 2.5 / 2.6 gateway paths)
- Rate limiting

### State / Persistence

All interfaces share a SQLAlchemy-managed database (SQLite for development, PostgreSQL for production). Tables include migration state, ID mappings, checkpoints, job records, job events, and saved connections.

## Containerized deployment

The `container/` directory contains everything needed to run the full stack:

| File | Role |
|------|------|
| `Containerfile.api` | Builds the FastAPI + dependency-analyzer backend image |
| `Containerfile.ui` | Multi-stage build: compiles the React app, serves via Nginx |
| `Containerfile` | CLI-only image for headless migrations |
| `docker-compose.yml` | Orchestrates PostgreSQL, API, and UI containers |
| `nginx.conf` | Reverse-proxies `/api` and `/ws` to the backend, serves static UI assets |

Run `podman compose up` from `container/` to start all three services.
