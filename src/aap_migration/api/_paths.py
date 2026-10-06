"""Routing-layer paths for the REST API.

Single home for the versioned route prefix. The background-jobs package
re-exports it for back-compat, but new code imports from here so a
route-version change never touches job internals.
"""

from __future__ import annotations

API_V1_PREFIX = "/api/v1"
