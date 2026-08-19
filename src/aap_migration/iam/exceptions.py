"""IAM analyser exceptions."""


class AuthenticationError(RuntimeError):
    """Raised when a target API call receives 401/403, aborting the migration."""

    def __init__(
        self,
        endpoint: str,
        status_code: int,
        *,
        entries_succeeded: int = 0,
        entries_remaining: int = 0,
    ):
        self.endpoint = endpoint
        self.status_code = status_code
        self.entries_succeeded = entries_succeeded
        self.entries_remaining = entries_remaining
        super().__init__(
            f"Target returned HTTP {status_code} on '{endpoint}' — "
            f"aborting migration. "
            f"{entries_succeeded} entries succeeded before failure, "
            f"{entries_remaining} remaining (will be idempotent no-ops on re-run)."
        )


class PaginationError(RuntimeError):
    """Raised when API pagination fails mid-stream or returns inconsistent counts."""

    def __init__(
        self,
        endpoint: str,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        items_collected: int = 0,
        expected_count: int | None = None,
    ):
        self.endpoint = endpoint
        self.url = url
        self.status_code = status_code
        self.items_collected = items_collected
        self.expected_count = expected_count
        detail = f"Pagination failed for '{endpoint}': {message}"
        if url:
            detail += f" (url={url})"
        if status_code is not None:
            detail += f" (HTTP {status_code})"
        detail += f" — {items_collected} items collected"
        if expected_count is not None:
            detail += f", server reported {expected_count}"
        super().__init__(detail)
