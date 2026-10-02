"""Credential endpoints (mirrors ``credentials compare|migrate|report``)."""

from __future__ import annotations

from fastapi import APIRouter

from aap_migration.api import services
from aap_migration.api.routers._common import submit_chained
from aap_migration.api.schemas import (
    CredentialCompareRequest,
    CredentialMigrateRequest,
    CredentialReportRequest,
    JobCreated,
)

router = APIRouter(tags=["credentials"])


@router.post("/credentials/compare", response_model=JobCreated, status_code=202)
def compare_credentials(body: CredentialCompareRequest) -> JobCreated:
    """Compare source/target credentials and write a comparison report."""
    return submit_chained("credentials-compare", body, services.run_credential_compare)


@router.post("/credentials/migrate", response_model=JobCreated, status_code=202)
def migrate_credentials(body: CredentialMigrateRequest) -> JobCreated:
    """Migrate missing credentials (+ org/credential-type deps)."""
    return submit_chained("credentials-migrate", body, services.run_credential_migrate)


@router.post("/credentials/report", response_model=JobCreated, status_code=202)
def credential_report(body: CredentialReportRequest) -> JobCreated:
    """Generate a credential status report (same worker as compare)."""
    return submit_chained("credentials-report", body, services.run_credential_compare)
