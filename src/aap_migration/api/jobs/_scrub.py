"""Secret redaction for persisted/served console output.

Console logs capture worker stdout/stderr which may echo tokens, keys,
or URL userinfo; every persist/serve path scrubs through here.
"""

from __future__ import annotations

import re

_SCRUB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]+"), "Bearer ***"),
    (re.compile(r"(?i)(token[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"), r"\1***"),
    (re.compile(r"(?i)((?:x-api-key|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"), r"\1***"),
    (re.compile(r"://[^/@:\s]+:[^/@\s]+@"), "://***@"),
)


def _scrub_output(text: str) -> str:
    """Redact bearer tokens, secrets, and URL userinfo (never raises)."""
    try:
        for pattern, repl in _SCRUB_PATTERNS:
            text = pattern.sub(repl, text)
        return text
    except Exception:
        return text
