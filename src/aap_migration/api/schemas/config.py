"""Config request schemas (part of api.schemas package)."""

from __future__ import annotations

from ._shared import ConnectionSelector


# -- config --------------------------------------------------------------
class ConfigValidateRequest(ConnectionSelector):
    check_connectivity: bool = False


class ConfigShowRequest(ConnectionSelector):
    pass
