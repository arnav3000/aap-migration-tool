"""Maintenance endpoints (mirrors ``prep`` and ``cleanup``)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter

from aap_migration.api import services
from aap_migration.api.jobs import get_job_manager
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import (
    CleanupRequest,
    JobCreated,
    PrepRequest,
    PrepSchemasOut,
)

router = APIRouter(tags=["maintenance"])


@router.post("/prep", response_model=JobCreated, status_code=202)
def start_prep(body: PrepRequest) -> JobCreated:
    """Discover endpoints + generate schemas (mirrors ``prep``)."""
    return submit_chained("prep", body, services.run_prep)


@router.get("/prep/schemas", response_model=PrepSchemasOut)
def get_prep_schemas() -> dict:
    """Return locally cached prep artifacts when present (startup-CWD bound).

    Always HTTP 200. Each artifact value is the parsed payload or null
    when missing/unreadable; per-file failures are surfaced in the
    top-level ``"errors_by_file": {name: message}`` map (never as
    ``{"error": ...}`` sentinels inside the values). ``"errors"`` is
    kept as a deprecated alias of ``errors_by_file`` for back-compat
    (deprecated=True in OpenAPI; removed in v2); new clients should read
    ``errors_by_file``. Validation results elsewhere use ``errors`` as a
    list of strings -- the names diverge on purpose and OpenAPI pins each
    route separately (see ``PrepSchemasOut``).
    """
    startup_cwd = Path(os.environ.get("AAP_BRIDGE_STARTUP_CWD", os.getcwd())).resolve()
    base = get_job_manager().base_dir
    out: dict = {}
    errors: dict = {}
    for name in (
        "schemas/source_endpoints.json",
        "schemas/target_endpoints.json",
        "schemas/schema_comparison.json",
    ):
        # Prefer startup-CWD artifacts (CLI parity), never the worker's CWD.
        candidates = [startup_cwd / name, Path(base).resolve() / name]
        payload = None
        for candidate in candidates:
            try:
                candidate.relative_to(startup_cwd)
            except ValueError:
                try:
                    candidate.relative_to(Path(base).resolve())
                except ValueError:
                    continue
            if candidate.is_file():
                try:
                    with open(candidate) as fh:
                        payload = json.load(fh)
                except Exception as exc:
                    payload = None
                    errors[name] = str(exc)
                break
        out[name] = payload
    out["errors_by_file"] = errors
    # Deprecated alias (deprecated=True in PrepSchemasOut; removed in v2).
    out["errors"] = errors
    return out


@router.post("/cleanup", response_model=JobCreated, status_code=202)
def start_cleanup(body: CleanupRequest) -> JobCreated:
    """Delete migrated resources + reset state (mirrors ``cleanup``)."""
    return submit_chained("cleanup", body, services.run_cleanup)
