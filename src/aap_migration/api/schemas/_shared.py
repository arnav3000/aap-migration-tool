"""Pydantic request/response schemas for the REST API.

Field names and defaults mirror the ``release/v1.x`` click options 1:1 so the
API is a faithful superset of the CLI surface.

Contract notes (error envelope vs result envelope):

- Error responses always use ``{"detail": "<human message>"}`` with a plain
  string ``detail`` (FastAPI ``HTTPException`` plus the app-level
  ``ValueError``/``KeyError`` handlers). The single ``422`` case that used to
  return a dict detail now returns a string; structured data travels in the
  success result or a documented ``result`` payload.
- Validation *results* (payload checks, dependency gates) use ``200`` with an
  explicit ``{"valid": bool, "errors": [...]}`` shape. That is a result, not
  an error envelope, and is documented per endpoint.
- Success *job results* share one envelope: every ``result`` dict carries
  ``message`` plus ``artifacts`` (possibly empty; workdir-relative paths).
  Per-type keys (``report``, ``summary``, ``stats``, ``migration_order``,
  ``comparison``, ...) ride alongside, never instead of, that pair.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatusValue = Literal["queued", "running", "succeeded", "failed", "cancelled"]
JobListFilter = JobStatusValue


# -- shared -------------------------------------------------------------
class ConnectionSelector(BaseModel):
    """Optionally override the active stored connections for one call."""

    model_config = {"extra": "forbid"}

    source_id: str | None = Field(
        default=None, description="Stored source connection id (default: active)"
    )
    target_id: str | None = Field(
        default=None, description="Stored target connection id (default: active)"
    )


class JobCreated(BaseModel):
    """Accepted-job response. Server-local paths are intentionally omitted."""

    job_id: str
    job_type: str
    status: JobStatusValue = "queued"
    poll_url: str = Field(
        default="",
        description="Host-relative poll path (e.g. /api/v1/jobs/<id>); "
        "resolve it against the API base URL in use.",
    )
    chained_from_status: JobStatusValue | None = Field(
        default=None,
        description="Status of the referenced job at submit time when chaining "
        "(echoes succeeded, failed, or cancelled on resume/retry paths; "
        "None when unchained)",
    )


class ChainedRequest(ConnectionSelector):
    """Requests that can continue work in an existing job directory.

    ETL phases share a working directory (exports/, xformed/, state DB).
    Pass the ``job_id`` of a previous job to chain phases, e.g. export, then
    ``POST /transforms {"job_id": "<export-job>"}``, then import, validate,
    and reporting against the same directory. Omit for a fresh isolated dir.

    Chaining requires the referenced job to have ``succeeded`` unless
    the endpoint chains onto failed/cancelled jobs (resume/retry) or
    ``allow_pair_switch``/partial-input semantics are explicitly opted into
    (see router errors). Switching AAP pairs mid-pipeline requires
    ``allow_pair_switch=true``.

    Unknown fields are rejected (``extra="forbid"`` inherited) so typos like
    ``resource_typs`` fail with 422 instead of running unscoped with 202.
    """

    job_id: str | None = Field(
        default=None,
        description="Existing job id whose working directory should be reused",
    )
    allow_pair_switch: bool = Field(
        default=False,
        description="Explicit opt-in to run a chained phase against a different AAP pair",
    )
    force: bool = Field(
        default=False,
        description="Explicit opt-in to chain onto a job whose previous attempt "
        "was cancelled mid-phase (cancel fence). Without force, chaining onto "
        "a mid-phase-cancelled job returns 409; resubmit explicitly instead. "
        "Subclasses may reuse this flag for overwrite semantics.",
    )


class JobStatus(BaseModel):
    """Pollable job state. No server-local filesystem paths are exposed."""

    job_id: str
    job_type: str
    status: JobStatusValue
    params: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error: str | None = None
    exit_code: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


def parse_organizations(params: dict[str, Any]) -> list[str] | None:
    """Canonical organization scope (single home, foundation leaf).

    Accepts legacy spellings at the boundary and returns the canonical
    ``organizations: list[str] | None`` where ``None`` means all (no filter).

    Accepted keys:

    - ``organizations``: ``list[str]`` (canonical) or comma-separated ``str``.
    - ``organization``: single ``str`` (or single-element ``list``).
    - ``orgs``: comma-separated ``str`` (CLI ``--orgs``) or ``list[str]``.

    Rules (fail closed, never silently widen to all):

    - No known org keys present (all missing/``None``/empty-string) -> ``None``.
    - Exactly one spelling present with values -> normalized ``list[str]``.
      Empty ``list`` is preserved as ``[]`` (explicit empty); empty/blank
      ``str`` normalizes to ``None`` (mirrors ``parse_orgs_arg``).
    - Multiple spellings present with values -> ``ValueError`` (ambiguous;
      specify only one spelling) instead of silently picking one.
    - Unknown ``*org*`` keys (e.g. ``organisations``, ``org``) holding a
      non-empty value -> ``ValueError`` instead of widening to ``None``
      (all). ``by_organization``/``analyze_all`` are exempt (flags, not
      scope values).

    Callers keep their own scope validation (e.g. analyze ``analyze_all``
    vs orgs exclusivity); this helper only normalizes the spelling.
    """
    _KNOWN = ("organizations", "organization", "orgs")
    _EXEMPT = {"analyze_all", "by_organization"}
    # Fail closed on typo spellings: an unknown *org* key with a real value
    # must not silently become None (= all organizations).
    for key, value in params.items():
        if "org" not in key.lower():
            continue
        if key in _KNOWN or key in _EXEMPT:
            continue
        if value is None:
            continue
        if isinstance(value, list | tuple) and len(value) == 0:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        raise ValueError(
            f"Unknown organization field {key!r}; "
            "use one of 'organizations', 'organization', or 'orgs'."
        )

    def _norm_list(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            return parts if parts else None
        if isinstance(value, list | tuple):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            if len(value) == 0:
                return []
            return cleaned if cleaned else []
        return None

    def _norm_single(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else None
        if isinstance(value, list | tuple):
            return _norm_list(value)
        return None

    normed: dict[str, list[str] | None] = {
        "organizations": _norm_list(params.get("organizations"))
        if "organizations" in params
        else None,
        "organization": _norm_single(params.get("organization"))
        if "organization" in params
        else None,
        "orgs": _norm_list(params.get("orgs")) if "orgs" in params else None,
    }
    present = {k: v for k, v in normed.items() if v is not None}
    # Treat explicit [] as "no values" for ambiguity: default [] plus one
    # real spelling is not ambiguous (mirrors Analyze default).
    with_values = {k: v for k, v in present.items() if len(v) > 0}
    if len(with_values) > 1:
        raise ValueError(
            "Ambiguous organization scope: specify only one of "
            "'organizations', 'organization', or 'orgs'."
        )
    if with_values:
        return next(iter(with_values.values()))
    for v in present.values():
        if v == []:
            return []
    return None
