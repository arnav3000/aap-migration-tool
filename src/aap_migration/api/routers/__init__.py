"""FastAPI routers for the AAP Bridge REST API."""

from aap_migration.api.routers import (
    analysis,
    config,
    connections,
    credentials,
    iam,
    jobs,
    maintenance,
    migrations,
    reporting,
    state,
    system,
    validation,
)

__all__ = [
    "analysis",
    "config",
    "connections",
    "credentials",
    "iam",
    "jobs",
    "maintenance",
    "migrations",
    "reporting",
    "state",
    "system",
    "validation",
]
