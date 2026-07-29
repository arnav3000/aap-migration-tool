# Migration state vs target existence

## Why the state database exists

Checking whether the target AAP already has an object named `X` is **necessary but not sufficient**.

Source payloads keep **source numeric foreign keys** (`organization: 5`, `credential: 12`). After create, those IDs differ on the target. Dependents must rewrite every FK to the **target** id.

The state database holds that graph:

| Store | Purpose |
|-------|---------|
| `id_mappings` | `source_id → target_id` (and names) per resource type |
| `migration_progress` | pending / completed / failed / skipped for resume and reporting |
| `checkpoints` | phase-level resume metadata |

Name-only existence on the target cannot replace mappings when:

1. Names collide across orgs or after `name_prefix`
2. Transform does not rewrite FKs — import resolves them via `get_mapped_id`
3. Multi-source planner runs scope maps with `source_key`

**Mental model:** state = this migration’s ID graph and progress; target name/list checks = safety net and bootstrap input.

## Hybrid checks today

1. Fast path: `state.is_migrated` → skip (no HTTP)
2. Else: `find_resource_by_name` / create with `check_exists`; on hit/409, write the mapping
3. **Target bootstrap** (pre-scan): list source + target, match by natural key, seed `id_mappings` before ETL so re-runs are not blind

## PostgreSQL is the state store

Production and local compose use PostgreSQL (`MIGRATION_STATE_DB_PATH`). The default DSN points at the compose `db` service on the host (`localhost:5432`). SQLite URLs remain usable for unit tests only; do not design features around SQLite limitations.
