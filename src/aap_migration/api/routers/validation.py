"""Validation endpoints (mirrors ``validate`` + dependency/payload checks)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from aap_migration.api import services
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import (
    DependencyCheckRequest,
    JobCreated,
    PayloadCheckOut,
    PayloadCheckRequest,
    ValidateRequest,
    ValidationDependencyOut,
)

log = logging.getLogger("aap_migration.api.validation")

router = APIRouter(tags=["validation"])


def _redacted_dir(path: str) -> str:
    """Return a job-tree-relative (or basename) directory, never absolute."""
    import os

    try:
        from aap_migration.api.jobs import get_job_manager

        base = os.path.abspath(get_job_manager().base_dir)
        target = os.path.abspath(path)
        if os.path.commonpath([target, base]) == base:
            return os.path.relpath(target, base)
    except Exception:
        pass
    return os.path.basename(path.rstrip(os.sep)) or path


@router.post("/validations", response_model=JobCreated, status_code=202)
def start_validation(body: ValidateRequest, request: Request) -> JobCreated:
    """Post-migration validation (database mode default, ``live`` optional)."""
    # skip_hosts/hosts exclusion lives in ValidateRequest (single home).
    return submit_chained(
        "validate", body, services.run_validate, root_path=request.scope.get("root_path", "")
    )


@router.post("/validations/dependencies", response_model=ValidationDependencyOut)
def check_dependencies(body: DependencyCheckRequest) -> dict:
    """Pre-import dependency gate (mirrors the import pre-flight check).

    Read-only: never creates the server-default state DB. Without
    ``job_id``, the closure is judged against the existing server-default
    DB when present, otherwise against an explicitly throwaway temp-dir
    state (empty: every dependency reports missing) so a mere check
    cannot flip later readers from 200-plus-warning to empty stats.
    """
    from contextlib import ExitStack

    from aap_migration.api.context import open_throwaway_state
    from aap_migration.validation import DependencyValidator

    with ExitStack() as stack:
        if body.job_id:
            from aap_migration.api.routers._common import resolve_chained_scope

            # Same 404/409 semantics as submit-time chaining; strict
            # xformed gate (P2 #17). Targetless: only explicit ids validated.
            _, chained_db, input_dir = resolve_chained_scope(
                body.job_id,
                body.source_id,
                body.target_id,
                require_xformed=True,
            )
            state = chained_db
        else:
            # Server-default scope is targetless: only the default
            # transform_dir is needed, so don't require a target pair
            # (source-only deployments must get 200, not 400).
            from aap_migration.config import PathConfig

            input_dir = PathConfig().transform_dir
            from aap_migration.api.context import default_state_db_path, open_state

            default_db = default_state_db_path()
            if default_db is not None:
                state = open_state(default_db)
            else:
                state = stack.enter_context(open_throwaway_state())
        if not os.path.isdir(input_dir):
            raise HTTPException(
                status_code=404,
                detail="Transformed data directory not found for this scope",
            )

        validator = DependencyValidator(state, Path(input_dir))
        if body.resource_types is not None and len(body.resource_types) == 0:
            return {
                "input_dir": _redacted_dir(input_dir),
                "validation": {"results": [], "ready": 0, "blocked": 0, "warnings": 0},
            }
        requested = list(body.resource_types) if body.resource_types is not None else None
        try:
            report = validator.validate_all(requested)
        except Exception as exc:
            log.exception("dependency validation failed")
            raise HTTPException(
                status_code=500, detail="Validation failed (see server logs for detail)"
            ) from exc
        # Serialize conservatively (rich objects may not be JSON-safe)
        try:
            import dataclasses

            if dataclasses.is_dataclass(report):
                payload = dataclasses.asdict(report)
            elif hasattr(report, "to_dict"):
                payload = report.to_dict()
            elif isinstance(report, dict):
                payload = report
            else:
                payload = {"report": str(report)}
        except Exception:
            payload = {"report": str(report)}
        return {"input_dir": _redacted_dir(input_dir), "validation": payload}


@router.post("/validations/payload", response_model=PayloadCheckOut)
def check_payload(body: PayloadCheckRequest) -> dict:
    """Validate a single resource payload (mirrors ``PayloadValidator``).

    Returns ``200`` with ``{"valid": bool, "errors": [...]}``. This is a
    validation *result*, not an error envelope: malformed requests still use
    ``400 {"detail": ...}``. Unknown ``resource_type`` is ``404`` (matching
    ``POST /transforms/preview`` and ``GET /resources/{type}``) so shared
    clients branch on one code for unknown-type.
    """
    from aap_migration.resources import get_info, normalize_resource_type
    from aap_migration.validation.payload_validator import PayloadValidator

    resource_type = normalize_resource_type(body.resource_type)
    try:
        get_info(resource_type)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown resource type '{body.resource_type}'"
        ) from exc
    validator = PayloadValidator()
    try:
        ok, errors = validator.validate_payload(resource_type, body.payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"resource_type": resource_type, "valid": bool(ok), "errors": errors}
