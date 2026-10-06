"""AAP Bridge REST API.

FastAPI-based REST API exposing every function/option/feature of the
``release/v1.x`` CLI (``aap-bridge``) as HTTP endpoints.

Long-running operations (export/transform/import/migrate/IAM/validate-live)
run as background jobs: ``POST`` returns a ``job_id`` which is polled via
``GET /api/v1/jobs/{job_id}``. Quick read-only operations return synchronously.

AAP connection settings (source/target URLs + tokens) are managed via
``/api/v1/connections`` and persisted encrypted in a small API database
(``AAP_BRIDGE_API_DB``, SQLite by default).
"""

__all__ = ["__version__"]

try:
    from importlib.metadata import version

    __version__ = version("aap-bridge")
except Exception:  # pragma: no cover - package metadata may be absent in dev
    __version__ = "0.0.0-dev"
