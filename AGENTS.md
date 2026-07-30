# AAP Bridge Agent Configurations

This document defines the specialized agents and architectural invariants for
AAP Bridge development. Read this before touching code.

## Product Focus

**AAP Bridge is a migration tool for Ansible Automation Platform.** Its job is
to provide everything needed for an AAP migration -- export, transform, import,
validate, and report on AAP resources between versions. Every feature must serve
this mission. Features that do not directly support AAP migration do not belong
in this tool.

### Scope Litmus Test

Before adding any feature, the agent (or human) must answer these questions:

1. Does this help export, transform, import, validate, or report on AAP resources?
2. Does this fix a real migration failure or gap?
3. Would removing this leave users unable to complete a migration?

If the answer to all three is **no**, the feature does not belong here. AAP Bridge
is not a general-purpose Ansible tool, not an AAP management UI, and not an
analytics platform.

## Architectural Invariants

These are non-negotiable. Violating any of them will break the system or create
debt that compounds across modules. Do **not** work around them.

1. **Dependency-ordered migration.** Resources are migrated in dependency order
   (organizations first, RBAC last). The dependency graph in the README is the
   source of truth. Do not change migration ordering without understanding the
   full dependency chain.

2. **Deferred credential patching, not credential-first.** The UI/TUI workflow
   imports resources first, then patches objects with credential references once
   credentials exist on the target. This pause-and-patch approach is the design.
   Do not rewrite it to require credentials before other resources.

3. **PostgreSQL is the state database.** State management uses SQLAlchemy with
   PostgreSQL (compose `db` service by default). SQLite URLs remain only for
   unit tests. Do not design around SQLite limitations.

4. **Idempotent migrations.** Running the same migration twice must produce the
   same result without creating duplicates. The state database tracks
   source-to-target ID mappings and checkpoint progress. Never bypass the state
   check.

5. **ETL pipeline isolation.** Export, Transform, and Import are separate phases
   with separate modules (`exporter.py`, `transformer.py`, `importer.py`). Each
   phase reads from and writes to disk (`exports/`, `xformed/`). Do not merge
   phases or create shortcuts that skip the disk boundary.

6. **Client layer separation.** Source client, target client, and vault client
   are independent. They share no state, no connection pools, and no
   configuration beyond what is injected via settings. Do not couple clients.

7. **CLI/API duality.** The same migration logic powers both the Click CLI/TUI
   and the FastAPI web API. Migration logic lives in `migration/`, not in `cli/`
   or `api/`. Do not put business logic in route handlers or Click commands.

8. **Resource type isolation.** Each AAP resource type has its own migration
   handling. Adding a new resource type should not require changes to existing
   resource migrators.

9. **Split-file architecture for large datasets.** Exports are split into
   multiple files when datasets exceed configurable thresholds. Import handles
   multi-file inputs transparently. Do not assume single-file exports.

10. **Container deployment via podman compose.** Production deployment uses
    `container/docker-compose.yml`. Container images are built from
    `container/Containerfile*`. Do not add Docker-only features.

11. **No scope creep.** See Product Focus above. Every new feature or change
    must serve AAP migration directly.

## Agent Roles

### 1. Migration Engine Agent

**Purpose**: Implements the core ETL pipeline for AAP resource migration.

**Scope**: `src/aap_migration/migration/`

**Owns**: coordinator, exporter, importer, transformer, parallel variants,
state management, checkpoint, credential comparator, database models.

**Constraints**:

- Must preserve dependency ordering (invariant 1)
- Must maintain idempotency via state database (invariant 4)
- ETL phases stay isolated with disk boundaries (invariant 5)
- Must not import from `cli/` or `api/` (invariant 7)

---

### 2. Client Agent

**Purpose**: Implements HTTP clients for source AAP, target AAP, and HashiCorp
Vault.

**Scope**: `src/aap_migration/client/`

**Owns**: source client, target client, vault client, retry logic, rate
limiting, AAP version detection, API path resolution (v2 vs controller/v2).

**Constraints**:

- Clients are independent -- no shared state or connection pools (invariant 6)
- Must handle AAP 2.4/2.5 and 2.6 API differences transparently
- Must implement retry with backoff (tenacity)
- Must never store credentials in logs or state

---

### 3. CLI Agent

**Purpose**: Implements the Click CLI, TUI menus, and interactive workflows.

**Scope**: `src/aap_migration/cli/`

**Owns**: Click commands, TUI menus, granular import, progress display,
decorators, context management, output formatting.

**Constraints**:

- No business logic in commands -- delegate to `migration/` (invariant 7)
- Must support all output modes: normal, quiet, CI/CD, detailed
- TUI must support pause-and-patch credential workflow (invariant 2)

---

### 4. API Agent

**Purpose**: Implements the FastAPI web API and WebSocket interface.

**Scope**: `src/aap_migration/api/`

**Owns**: FastAPI app, routers (analysis, connections, jobs, migration),
services, WebSocket real-time progress, schemas, crypto.

**Constraints**:

- No business logic in route handlers -- delegate to `migration/` (invariant 7)
- WebSocket provides real-time progress; it does not drive migration logic
- Must validate all inputs via Pydantic schemas

---

### 5. Analysis Agent

**Purpose**: Implements pre-migration analysis and post-migration reporting.

**Scope**: `src/aap_migration/analysis/`

**Owns**: dependency analyzer, dependency graph, quality checks, HTML reports,
text reports.

**Constraints**:

- Analysis is read-only -- never modifies source or target AAP
- Reports must be self-contained (HTML or Markdown, no external dependencies)

---

### 6. Infrastructure Agent

**Purpose**: Manages containers, CI/CD, web frontend, and build tooling.

**Scope**: `container/`, `.github/workflows/`, `scripts/`, `web/`, `Makefile`,
`.pre-commit-config.yaml`

**Owns**: Containerfiles, docker-compose, GitHub Actions workflows, Makefile
targets, pre-commit configuration, frontend build.

**Constraints**:

- All CI steps must be reproducible locally via `make <target>`
- Container images use `container/Containerfile*` (not Dockerfile)
- Pin GitHub Actions to commit SHAs, not mutable tags
- Do not add setup actions beyond what exists without justification

---

## Quality Gates

All agents must run these before committing:

| Command | What it does | When to run |
|---------|-------------|-------------|
| `make check` | format + lint + typecheck + test | Before every commit |
| `make pre-commit` | All pre-commit hooks on all files | Before every commit |
| `make c-check` | Containerized lint + typecheck + full test suite | Before PRs |
| `make c-ci-full` | Full containerized regression with 80% coverage threshold | CI gate |

### Prohibited direct invocations

**Do not run any of these directly. Use the corresponding make target.**

| Prohibited | Use instead |
|------------|-------------|
| `pytest ...` | `make test` or `make test-unit` |
| `ruff check ...` | `make lint` |
| `black ...` | `make format` |
| `mypy ...` | `make typecheck` |
| `pre-commit run ...` | `make pre-commit` |

## Key Source Layout

```
src/aap_migration/
├── analysis/          # Dependency analysis, quality, reports
│   ├── dependency_analyzer.py
│   ├── dependency_graph.py
│   ├── graph.py
│   ├── html_report.py
│   ├── quality.py
│   ├── reports.py
│   └── text_report.py
├── api/               # FastAPI REST + WebSocket
│   ├── routers/       # analysis, connections, jobs, migration
│   ├── services/      # Business logic adapters
│   ├── app.py
│   ├── crypto.py
│   ├── schemas.py
│   └── websocket.py
├── cli/               # Click CLI + TUI
│   ├── commands/      # analyze_dependencies, cleanup, config, credentials,
│   │                  # export_import, migrate, migration_report, patch_projects,
│   │                  # prep, project_failures, retry, serve, state, transform
│   ├── main.py
│   ├── menu.py
│   ├── granular_import.py
│   ├── import_menu.py
│   └── utils.py
├── client/            # HTTP clients (source, target, vault)
├── migration/         # Core ETL: export, transform, import, state
│   ├── coordinator.py
│   ├── credential_comparator.py
│   ├── database.py
│   ├── exporter.py
│   ├── importer.py
│   ├── models.py
│   ├── parallel_exporter.py
│   ├── parallel_transformer.py
│   ├── state.py
│   ├── checkpoint.py
│   └── transformer.py
├── prep/              # Pre-migration preparation
├── schema/            # AAP schema discovery
├── sizing/            # Dynamic sizing calculations
├── utils/             # Shared utilities
├── validation/        # Post-migration validation
├── config.py          # Configuration models (pydantic-settings)
└── resources.py       # Resource type definitions
```

## Design Thinking

### Sunk cost fallacy

Do not defend existing code simply because effort was invested in it. If a
fix requires increasingly complex workarounds -- offset detection, heuristic
correction, retry loops -- the underlying abstraction is likely wrong.
Discard the existing approach and redesign the interface.

**Two workarounds for the same interface = redesign the interface.**

### Treat directional feedback as architectural

When a human says "we're too coupled to X" or "why do we need Y," treat
it as an architectural concern, not a narrow bug. Step back to first
principles before writing code. Ask: *"What would this look like if we
didn't have X at all?"*

### Two failed attempts = wrong abstraction

If the same class of failure recurs after two fix attempts, do not attempt
a third fix at the same level. Escalate to a design review of the
interface itself. The pattern of repeated failure is the evidence.

### No scope creep

Before adding a feature, apply the scope litmus test. If it does not serve
AAP migration directly, it does not belong. This tool migrates AAP -- that
is its entire purpose. Resist the temptation to add "nice to have" features
that dilute focus.

### Migration correctness over speed

Correctness -- idempotency, dependency ordering, no data loss -- always wins
over performance. Optimize only after correctness is proven. A migration
that loses data quickly is worse than one that runs slowly but safely.

## Project Skills

This project defines agent skills in `.agents/skills/`. When the user types a
`/slash-command`, check `.agents/skills/<command-name>/SKILL.md` **before doing
anything else**. If a matching skill exists, read it and follow its instructions.

| Command | Purpose |
|---------|---------|
| `/branch-align` | Align branch name after renaming |
| `/lean-ci` | CI workflow guidance |
| `/make` | Makefile target reference (lint, test, build, container) |
| `/migration-test` | Guide for writing migration tests |
| `/pr-contributor-review` | Review external contributor PRs |
| `/pr-new` | Create and submit pull requests |
| `/pr-review` | Handle PR review feedback |
| `/scope-guard` | Evaluate proposed features against project mission |
| `/security-scan` | Scan dependencies and CI for vulnerabilities |
