## Task 1 Report — Wire `host_inventory_memberships` Export Path

**Status:** DONE

### What was done

Added `export_parallel()` override to `HostInventoryMembershipExporter` in
`src/aap_migration/migration/exporter.py` (after line 1175, before `CredentialExporter`).

The override delegates to `self.export(filters=filters)` instead of the base class
implementation that would call `GET ""` (API root) with the empty endpoint.

### Test

- File: `tests/unit/test_host_memberships.py`
- Class: `TestHostInventoryMembershipExporterParallel`
- Test: `test_export_parallel_delegates_to_export`
- Confirmed FAIL before implementation: `assert 0 == 2` (base class yields nothing for empty endpoint)
- Confirmed PASS after implementation: 1 passed

**Note on async marker:** `pytest-asyncio` is not installed in `.venv-api` (only `anyio==4.13.0`
is present). Used `@pytest.mark.anyio` (provided by the installed `anyio` package) instead of
`@pytest.mark.asyncio` as specified in the brief. Functional behaviour is identical.

### Implementation added

```python
async def export_parallel(
    self,
    resource_type: str,
    endpoint: str,
    page_size: int = 200,
    max_concurrent_pages: int = 5,
    filters: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """Delegate to export() — no single endpoint exists for memberships."""
    async for record in self.export(filters=filters):
        yield record
```

### Files modified

- `src/aap_migration/migration/exporter.py` — added `export_parallel` override
- `tests/unit/test_host_memberships.py` — new test file (created)
