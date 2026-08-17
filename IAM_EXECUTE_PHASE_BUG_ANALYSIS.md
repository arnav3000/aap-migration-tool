# IAM Execute Phase — Bug Analysis

**Date:** 2026-08-14  
**Component:** `src/aap_migration/iam/analyser.py`  
**Severity:** Critical (BUG-1), High (BUG-2, BUG-3), Low (BUG-4)  
**Status:** Open — no fixes applied

---

## Executive Summary

Three bugs in the IAM execute phase cause migrations with large permission sets to take weeks instead of hours, silently lose progress on every interruption, and silently drop permissions for affected resources with a misleading error message. A fourth minor issue creates incorrect user expectations about the `--workers` flag.

Evidence comes from a live migration dataset: 261,868 permissions, 4 interruptions across 14+ days of running, and zero persistently recorded permissions applied despite the process actively making API calls.

---

## BUG-1 — Execute Phase Has No Checkpointing (Critical)

### Location

`src/aap_migration/iam/analyser.py:1462–1586` — `_migrate_permissions()`

### Description

The scan phase (`scan_permissions_principal`) saves a checkpoint after every batch of principals. The execute phase (`_migrate_permissions`) has **no checkpoint saves at all**. Permission statuses are updated in memory only — never written to disk. When the process is interrupted (Ctrl+C, SIGKILL, OOM, token expiry), all execute progress is lost and the next run starts from scratch.

### Evidence From Code

```python
# analyser.py:1462 — _migrate_permissions
def _migrate_permissions(
    self,
    permissions: list[PermissionEntry],
    stats: MigrationStats,
    dry_run: bool = False,
) -> None:
    ...
    for idx, entry in enumerate(permissions):   # line 1472
        ...
        entry.status = "migrated"               # updated in memory only
        stats.permissions_migrated += 1
        time.sleep(self.rate_limit_delay)        # line 1580
    # ← function ends here — no _save_checkpoint() call anywhere
```

`_save_checkpoint()` is called 0 times inside `_migrate_permissions`. Confirmed by searching the full function body (lines 1462–1586).

### Evidence From Dataset

All 4 checkpoint files across the migration show 100% of permissions as `pending`:

| Checkpoint file | Updated at | Permissions | Status |
|---|---|---|---|
| `logs/checkpoint_iam/iam_checkpoint.json` | 2026-07-28 18:08 | 261,868 | 100% pending |
| `reports/local_migrate_OLD/iam_checkpoint.json` | 2026-08-06 04:20 | 262,484 | 100% pending |
| `reports/local_migrate_8thAug/iam_checkpoint.json` | 2026-08-07 11:17 | 262,774 | 100% pending |
| `reports/local_migrate/iam_checkpoint.json` | 2026-08-08 12:04 | 262,776 | 100% pending |

The process ran continuously from 2026-07-28 to 2026-08-08 (10 days), actively POSTing permissions to the target. Yet every checkpoint shows zero progress because `_migrate_permissions` never persists its work.

### Timeline of Interruptions

| Date | Event | Progress lost |
|---|---|---|
| 2026-07-26 09:24 | Ctrl+C during scan (team object_roles pagination) | Scan partial |
| 2026-07-28 | Resumed and completed scan | — |
| 2026-08-08 11:14 | **Ctrl+C during execute** (mid-SSL POST to target) | 10+ days of execute work |
| 2026-08-09 11:49 | Silent SIGKILL (no traceback in logs) | Scan re-run partially lost |

The Aug 8 interruption was confirmed from the migration log traceback:
```
File "analyser.py", line 1782, in migrate
    self._migrate_permissions(permissions, stats, dry_run=dry_run)
File "analyser.py", line 1548, in _migrate_permissions
    resp = self._target_post(endpoint, {"id": target_principal_id})
...
ssl.py:1115 recv_into() ← interrupted mid-SSL read
KeyboardInterrupt → click.exceptions.Abort
```

### Impact

- If the run **completes without interruption**: `IAMAuditResult` is returned to the CLI and written to HTML/JSON output files. No data loss — the checkpoint not updating is only a problem for resume.
- If the run **is interrupted**: all in-memory permission statuses are lost. Output files are not written. Checkpoint unchanged. Full restart required.
- Every interruption restarts execute from permission 0.
- On a 261,868-permission dataset at ~2.5s/permission, one restart = ~7.6 days of lost work.
- There is no way for operators to know how far execute got before stopping.
- Permissions may have been applied to the target but are not recorded. Re-running is functionally safe — duplicate assignments return HTTP 204, caught at `analyser.py:1559` and correctly marked `migrated`. The `"already"` string check at line 1564 is likely dead code for this operation.

### Fix

Save a checkpoint every N permissions (configurable, default 100) inside `_migrate_permissions`:

```python
# analyser.py:1472 — add checkpoint every N entries
CHECKPOINT_INTERVAL = 100

for idx, entry in enumerate(permissions):
    # Skip already-completed entries on resume
    if entry.status in ("migrated", "failed", "skipped", "dry_run"):
        continue

    ... # existing logic

    time.sleep(self.rate_limit_delay)

    # Checkpoint every N permissions
    if (idx + 1) % CHECKPOINT_INTERVAL == 0:
        self._save_checkpoint(permissions, stats, ...)
```

Additionally, add a status check at the top of the loop so that `--resume` correctly skips permissions already applied in a previous run:

```python
for idx, entry in enumerate(permissions):
    if entry.status in ("migrated", "failed", "skipped"):
        continue   # already done in a prior run
    ...
```

---

## BUG-2 — Execute Phase Is Sequential; `--workers` Has No Effect On It (High)

### Location

`src/aap_migration/iam/analyser.py:1462` — `_migrate_permissions()`  
`src/aap_migration/cli/commands/iam.py:181` — `--workers` CLI option

### Description

`_migrate_permissions` is a plain sequential `for` loop. The `--workers` flag is accepted by the CLI and passed to `IAMAnalyser(max_workers=workers)`, but `max_workers` is only used inside `scan_permissions()` and `scan_permissions_principal()` via `ThreadPoolExecutor`. The execute phase ignores it entirely.

Operators running `iam migrate --workers 10` reasonably expect 10x throughput improvement. They get none.

### Evidence From Code

**CLI definition** (`iam.py:181`):
```python
@click.option(
    "--workers",
    type=int,
    default=1,
    help="Concurrent workers for role membership scanning.",  # ← "scanning" not "execute"
)
```

**Analyser constructor** (`analyser.py:117`):
```python
max_workers: int = 1,
...
self.max_workers = max(1, max_workers)   # stored but...
```

**Scan phase** (`analyser.py:860`): `ThreadPoolExecutor(max_workers=self.max_workers)` — used here.

**Execute phase** (`analyser.py:1472`):
```python
for idx, entry in enumerate(permissions):   # plain sequential loop — max_workers never referenced
    ...
    time.sleep(self.rate_limit_delay)
```

`self.max_workers` is never referenced inside `_migrate_permissions`. Confirmed by searching the full function body.

### Performance Impact

With `rate_limit_delay=0.15s` (hardcoded default, `analyser.py:116`) and typical API response time:

| Permissions | Sequential time | With 10 workers (if fixed) |
|---|---|---|
| 81,378 (team perms) | ~2.4 days | ~6 hours |
| 180,490 (user perms) | ~5.2 days | ~13 hours |
| 261,868 (total) | ~7.6 days | ~18 hours |

### Fix

Wrap `_migrate_permissions` in a `ThreadPoolExecutor` using the existing `max_workers` attribute, following the same pattern already used in the scan phase:

```python
# analyser.py:1462 — parallelise with existing max_workers
def _migrate_permissions(self, permissions, stats, dry_run=False):
    ...
    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
        futures = {
            executor.submit(self._apply_single_permission, entry, dry_run): entry
            for entry in permissions
            if entry.status not in ("migrated", "failed", "skipped")
        }
        for future in as_completed(futures):
            result_entry = futures[future]
            try:
                future.result()
            except Exception:
                result_entry.status = "failed"
                stats.permissions_failed += 1
```

**Note:** BUG-1 (checkpointing) must be fixed alongside this — parallel execution without checkpointing still loses all work on interruption.

---

## BUG-3 — `object_roles/` HTTP 404 Silently Drops All Permissions For A Resource (High)

### Location

`src/aap_migration/iam/analyser.py:1516–1540` — inside `_migrate_permissions()`

### Description

During execute, for each permission entry the code calls `{resource_type}/{target_id}/object_roles/` on the **target** to look up the role ID. If this returns HTTP 404, `_target_paginate` returns an empty list (this is the correct graceful handling in `_paginate` at line 301–308). The empty list becomes an empty role cache. Every subsequent permission for that same resource then fails with:

```
Role '{role_name}' not found on target resource
```

This error message is **misleading** — the actual cause is that `object_roles/` returned 404, not that the role is missing. The operator has no way to distinguish "role genuinely missing" from "endpoint returned 404" without reading the source code.

Furthermore, **all permissions for that resource are dropped silently**. There is no retry, no fallback lookup by role name, and no distinct error code for the 404 case.

### Evidence From Code

```python
# analyser.py:1516
cache_key = f"{entry.resource_type}/{target_resource_id}"
if cache_key not in target_role_cache:
    roles_data = self._target_paginate(              # line 1518
        f"{entry.resource_type}/{target_resource_id}/object_roles/"
    )
    # If 404 → _paginate returns [] → cache = {}
    target_role_cache[cache_key] = {
        r["name"]: r["id"] for r in roles_data      # → empty dict
    }

target_role_id = target_role_cache.get(cache_key, {}).get(mapped_role)
# → None (because cache is empty)

if not target_role_id:
    entry.status = "failed"
    entry.error = (
        f"Role '{entry.role_name}' not found on target resource"  # misleading
    )
    stats.permissions_failed += 1
    continue
```

### Evidence From Dataset

Migration log shows HTTP 404 on specific target team IDs during the execute phase:

```
2026-08-09T11:17:24Z  WARNING  Paginate teams/5484/object_roles/ returned HTTP 404
2026-08-09T11:20:04Z  WARNING  Paginate teams/5482/object_roles/ returned HTTP 404
2026-08-09T11:43:22Z  WARNING  Paginate teams/5462/object_roles/ returned HTTP 404
2026-08-09T11:43:24Z  WARNING  Paginate teams/5480/object_roles/ returned HTTP 404
2026-08-09T11:49:50Z  WARNING  Paginate teams/5488/object_roles/ returned HTTP 404
```

These are confirmed **target** IDs — all are valid entries in `id_mappings`:

| Source ID | Target ID | Team name |
|---|---|---|
| 3727 | 5484 | MX_GSP_UAT_User |
| 3811 | 5482 | IWPB_Browser_Banking_UAT_User |
| 3908 | 5462 | BUS_PROCESS_ORCH_UAT_User |
| 3916 | 5480 | HK_ACCT_OPEN_ORIG_UAT_User |
| 4054 | 5488 | EMEA_KTC_AUTOMATION |

All teams migrated successfully (`migration_progress.status = completed`). The 404 is on the `object_roles/` endpoint for those target team IDs during permission apply. Root cause of why these specific teams return 404 is **unverified** — it requires calling the target API directly.

### Impact

Every permission assigned to these teams is marked failed with a misleading error. Operators investigating the failure will look for a missing role, not a 404 endpoint — wasting time on the wrong investigation path.

### Fix

Distinguish the 404 case explicitly, log the correct root cause, and optionally retry after a delay to handle propagation lag:

```python
# analyser.py:1517 — distinguish 404 from empty roles
cache_key = f"{entry.resource_type}/{target_resource_id}"
if cache_key not in target_role_cache:
    roles_data = self._target_paginate(
        f"{entry.resource_type}/{target_resource_id}/object_roles/"
    )
    if not roles_data:
        # Distinguish: was this a 404, or does the resource genuinely have no roles?
        # Mark with a sentinel so we don't retry repeatedly
        target_role_cache[cache_key] = None   # None = endpoint failed, {} = empty roles
    else:
        target_role_cache[cache_key] = {r["name"]: r["id"] for r in roles_data}

cached = target_role_cache.get(cache_key)
if cached is None:
    entry.status = "failed"
    entry.error = (
        f"object_roles/ returned HTTP 404 for {entry.resource_type} "
        f"target_id={target_resource_id} — resource may not be fully "
        f"initialised on target"
    )
    stats.permissions_failed += 1
    continue
```

---

## BUG-4 — `--workers` Help Text Does Not State It Applies To Scan Only (Low)

### Location

`src/aap_migration/cli/commands/iam.py:181`

### Description

The `--workers` help text says "Concurrent workers for role membership scanning." This is accurate but easy to misread — operators running `iam migrate --workers 10` assume it improves the overall migrate command, not just the internal scan sub-phase. When BUG-2 is fixed and execute becomes parallel, this text should be updated to reflect both phases.

### Current Text

```
"Concurrent workers for role membership scanning."
```

### Suggested Text (after BUG-2 is fixed)

```
"Concurrent workers for scan and execute phases (default: 1). 
 Use 'iam benchmark' to find the optimal value for your environment."
```

---

## Summary Table

| Bug | Location | Severity | Impact | Fix Complexity |
|---|---|---|---|---|
| BUG-1: No execute checkpointing | `analyser.py:1462` | **Critical** | Every interruption = full restart | Medium (2–3 days) |
| BUG-2: Sequential execute / workers ignored | `analyser.py:1472` | **High** | 7.6 days instead of ~18 hours | Medium (2–3 days, depends on BUG-1) |
| BUG-3: 404 silently drops permissions | `analyser.py:1518` | **High** | Permissions lost with misleading error | Low (4–8 hours) |
| BUG-4: Workers help text ambiguity | `iam.py:181` | Low | Operator confusion only | Trivial (30 min) |

---

## Recommended Fix Order

1. **BUG-3 first** — Smallest change, immediate value, no dependencies. Correct the 404 handling and error message. Operators can immediately distinguish 404 failures from missing-role failures.

2. **BUG-1 second** — Add checkpointing to `_migrate_permissions`. Must be done before BUG-2 — parallel execute without checkpointing would make interruptions more catastrophic, not less.

3. **BUG-2 third** — Parallelise execute using existing `ThreadPoolExecutor` pattern. Requires BUG-1 to be in place first.

4. **BUG-4 last** — Update help text after BUG-2 is fixed.

---

## Workaround (Without Code Changes)

Until fixes are applied:

1. Use `--resume` with the existing scan checkpoint to skip the re-scan
2. Split the run: `--skip-user-roles` first (81,378 permissions), then `--users-only` (180,490 permissions)
3. Run with `nohup` to prevent terminal-disconnect kills
4. Verify bearer token TTL before each run — token expiry mid-run causes `AuthenticationError` and restarts from zero
5. Accept that permissions for the ~10 affected teams will be marked failed until BUG-3 is fixed

---

*Analysis based on code at branch `feature/validate`, commit `7c44563`.*  
*Evidence verified from migration dataset (anonymised). No customer-identifiable data included.*
