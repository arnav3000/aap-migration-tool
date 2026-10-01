# Consolidated Migration Branches

This document records how the `feat/consolidated-migration` branch was assembled
from the following feature branches so they can be retired while preserving
their functionality for future cherry-picks.

## Source Branches

| Branch | Role in consolidation |
|--------|----------------------|
| `test/merge` | **Primary base** — full web UI, API, planner, selective JT migration, container stack, and latest migration fixes |
| `test2/merge` | Fully contained in `test/merge` (no unique commits) |
| `feat/selective-jt-migration` | Merged into `test/merge` via `89cbddf`; selective template migration with inventory sources and name prefix |
| `fix/planner-phase-execute-job-fk` | Merged into `test/merge` via `43cdb6a`; planner FK race fix, credential pause, Postgres volume pinning |
| `feature/validate-rebuild` | Validate API/CLI/UI and IAM web UI added on top of base |
| `feat/rebuild` | REST API layer — superseded by `test/merge` (no unique functionality) |
| `feature/validate-backup` | Snapshot of old `feature/validate` on `release/v1.x`; no commits ahead of `main` |

## What landed from each area

### From `test/merge` (base)

- React + PatternFly web UI with planner, migrate, operations, analysis, sizing
- FastAPI backend with migration planner, selective JT migration, job management
- Containerized dev stack with PostgreSQL state persistence
- Migration engine improvements (FK recovery, credential types, name prefix, etc.)

### From `feature/validate-rebuild`

- `src/aap_migration/validate/` — post-migration validation engine
- `src/aap_migration/api/routers/validate.py` + `validate_service.py`
- `src/aap_migration/cli/commands/validate.py` (`aap-bridge validate`)
- `web/src/pages/Validate.tsx` — validation UI
- `web/src/pages/IAM.tsx` — IAM audit/migrate/benchmark UI
- `src/aap_migration/api/services/iam_service.py` + `/iam/audit`, `/iam/migrate` endpoints

### Not included (intentionally)

- `feature/validate-backup` — stale backup branch, 35 commits behind `main`
- `feat/rebuild` — single "adding REST API" commit already present in `test/merge`
- Divergent copies of migration engine files from `fix/planner` and `selective-jt`
  that were re-implemented in `test/merge` with equivalent fixes

## Cherry-picking guide

To extract a feature later:

| Feature | Key paths / commits |
|---------|---------------------|
| Selective JT migration | `web/src/pages/Operations.tsx`, `src/aap_migration/api/routers/migration.py` |
| Planner FK fix | `src/aap_migration/api/routers/planner.py`, `src/aap_migration/migration/importer.py` |
| Validate | `src/aap_migration/validate/`, `api/routers/validate.py`, `web/src/pages/Validate.tsx` |
| IAM web UI | `web/src/pages/IAM.tsx`, `api/services/iam_service.py` |

## Branches safe to delete after merge

Once `feat/consolidated-migration` is reviewed and pushed:

- `test/merge`
- `test2/merge`
- `feature/validate-backup`
- `feat/rebuild`
- `feature/validate-rebuild`
- `fix/planner-phase-execute-job-fk`
- `feat/selective-jt-migration`
