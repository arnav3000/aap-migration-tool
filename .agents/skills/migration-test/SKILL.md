---
name: migration-test
description: Guide for writing migration engine tests. Use when adding tests for export, transform, import, state, or coordinator behavior.
---

# Migration Test

## Purpose

Write tests that prove migration correctness: idempotency, dependency order, ETL isolation, and state tracking.

## When to use

- Adding importer/exporter/transformer tests
- Testing `MigrationState`, checkpoint, or parallel coordinators
- Integration tests for disk ETL (`exports/`, `xformed/`)

## Patterns

1. **Unit tests** — `FakeState`, `FakeTargetClient`, `AsyncMock` in `tests/test_migration_importer_core.py` and siblings.
2. **Real state DB** — use `sqlite_db_url` fixture (SQLite for unit tests only per AGENTS.md invariant 3).
3. **Idempotency** — import the same resource twice; second call must not create duplicates (`tests/test_import_idempotency.py`).
4. **Markers** — `integration`, `requires_aap`, `requires_vault` for live-environment tests (opt-in).
5. **Run tests** — `make test-unit` or `make test`; never raw `pytest`.

## Key modules under test

- `src/aap_migration/migration/` — coordinator, exporter, importer, transformer, state
- `src/aap_migration/migration/phases.py` — phase order (RBAC last)
- `src/aap_migration/migration/runner.py` — disk import/export aggregation

## References

- [AGENTS.md](../../../AGENTS.md) — architectural invariants
- [Makefile](../../../Makefile) — `make test-unit`, `make migration-test` N/A; use `make test-unit`
- [tests/conftest.py](../../../tests/conftest.py) — `sqlite_db_url` fixture
