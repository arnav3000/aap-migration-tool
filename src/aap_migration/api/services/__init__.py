"""Background worker functions for API jobs, split by job family.

Each ``run_*`` function takes the job record (``job["params"]`` + internal
``job_dir``), builds an isolated :class:`MigrationContext` with absolute
paths only (never chdir), and either invokes the underlying click commands
programmatically or calls the service layer directly.

This package re-exports every ``run_*`` worker (plus the shared
:class:`JobParams` shape and :func:`chained_ctx` lifecycle) so existing
``services.run_x`` attribute access keeps working; new code should import
from the family module directly (``etl``, ``iam``, ``credentials``,
``reporting``, ``maintenance``).
"""

from __future__ import annotations

from aap_migration.api.services._core import JobParams, chained_ctx
from aap_migration.api.services.credentials import (
    run_credential_compare,
    run_credential_migrate,
)
from aap_migration.api.services.etl import (
    run_export,
    run_granular_import,
    run_import,
    run_migrate,
    run_migrate_resume,
    run_patch_projects,
    run_transform,
)
from aap_migration.api.services.iam import (
    run_iam_audit,
    run_iam_benchmark,
    run_iam_migrate,
    run_iam_report,
)
from aap_migration.api.services.maintenance import (
    run_cleanup,
    run_prep,
    run_retry_failed,
    run_state_export,
)
from aap_migration.api.services.reporting import (
    run_analyze_dependencies,
    run_enhanced_report,
    run_migration_report,
    run_project_failures,
    run_validate,
)

__all__ = [
    "JobParams",
    "chained_ctx",
    "run_analyze_dependencies",
    "run_cleanup",
    "run_credential_compare",
    "run_credential_migrate",
    "run_enhanced_report",
    "run_export",
    "run_granular_import",
    "run_iam_audit",
    "run_iam_benchmark",
    "run_iam_migrate",
    "run_iam_report",
    "run_import",
    "run_migrate",
    "run_migrate_resume",
    "run_migration_report",
    "run_patch_projects",
    "run_prep",
    "run_project_failures",
    "run_retry_failed",
    "run_state_export",
    "run_transform",
    "run_validate",
]
