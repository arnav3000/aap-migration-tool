"""FastAPI routers for the AAP Bridge REST API (stack 4 subset).

Full router set lands with stack 5; this subset exposes system, jobs,
connections, and config only.
"""

from aap_migration.api.routers import config, connections, jobs, system

__all__ = [
    "config",
    "connections",
    "jobs",
    "system",
]
