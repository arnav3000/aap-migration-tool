"""
Automation Hub specific exceptions.
"""


class AutomationHubError(Exception):
    """Base exception for Automation Hub operations."""

    pass


class GalaxyAPIError(AutomationHubError):
    """Exception raised when Galaxy API calls fail."""

    def __init__(self, message: str, status_code: int | None = None, response: dict | None = None):
        self.status_code = status_code
        self.response = response
        super().__init__(message)


class NamespaceError(AutomationHubError):
    """Exception raised for namespace-related errors."""

    pass


class CollectionError(AutomationHubError):
    """Exception raised for collection-related errors."""

    pass


class ArtifactError(AutomationHubError):
    """Exception raised for artifact (tarball) related errors."""

    pass


class RepositoryError(AutomationHubError):
    """Exception raised for repository-related errors."""

    pass


class RemoteRegistryError(AutomationHubError):
    """Exception raised for remote registry-related errors."""

    pass


class TaskTimeoutError(AutomationHubError):
    """Exception raised when Pulp async task times out."""

    pass


class TaskFailedError(AutomationHubError):
    """Exception raised when Pulp async task fails."""

    def __init__(self, message: str, task_data: dict | None = None):
        self.task_data = task_data
        super().__init__(message)
