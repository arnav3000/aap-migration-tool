"""
Data models for Automation Hub objects.

These models represent Ansible Collections, Namespaces, Repositories,
and related objects in Automation Hub.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Namespace:
    """Automation Hub Namespace.

    A namespace is an organizational unit that contains collections.
    Examples: ansible, community, redhat, myorg
    """

    name: str
    company: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    description: Optional[str] = None
    resources: Optional[str] = None
    links: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # Tracking IDs
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to API format for creation."""
        return {
            "name": self.name,
            "company": self.company,
            "email": self.email,
            "avatar_url": self.avatar_url,
            "description": self.description,
            "resources": self.resources,
            "links": self.links,
        }

    @classmethod
    def from_api(cls, data: dict) -> "Namespace":
        """Create from API response."""
        return cls(
            name=data["name"],
            company=data.get("company"),
            email=data.get("email"),
            avatar_url=data.get("avatar_url"),
            description=data.get("description"),
            resources=data.get("resources"),
            links=data.get("links", []),
            metadata=data,
            source_id=data.get("id"),
        )


@dataclass
class Collection:
    """Ansible Collection.

    Represents a collection (e.g., ansible.posix, community.general).
    A collection can have multiple versions.
    """

    namespace: str
    name: str
    description: Optional[str] = None
    deprecated: bool = False

    # Computed fields
    fqn: Optional[str] = None  # Fully Qualified Name: namespace.name

    # Versions
    latest_version: Optional[str] = None
    versions: list["CollectionVersion"] = field(default_factory=list)

    # Metadata
    download_count: int = 0
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Compute FQN if not provided."""
        if not self.fqn:
            self.fqn = f"{self.namespace}.{self.name}"

    @classmethod
    def from_api(cls, data: dict) -> "Collection":
        """Create from API response."""
        return cls(
            namespace=data["namespace"],
            name=data["name"],
            description=data.get("description"),
            deprecated=data.get("deprecated", False),
            latest_version=data.get("latest_version", {}).get("version"),
            download_count=data.get("download_count", 0),
            tags=data.get("tags", []),
            metadata=data,
        )


@dataclass
class CollectionVersion:
    """Specific version of a collection.

    Represents a particular version of a collection (e.g., ansible.posix:1.2.0).
    Each version has its own artifact (tarball) and metadata.
    """

    namespace: str
    name: str
    version: str

    # Artifact info
    artifact_url: Optional[str] = None
    artifact_sha256: Optional[str] = None
    artifact_size: Optional[int] = None

    # Metadata
    dependencies: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)

    # Local storage
    local_path: Optional[str] = None
    downloaded: bool = False
    uploaded: bool = False

    # Tracking
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    @property
    def fqn(self) -> str:
        """Fully Qualified Name: namespace.name"""
        return f"{self.namespace}.{self.name}"

    @property
    def full_name(self) -> str:
        """Full name with version: namespace.name:version"""
        return f"{self.fqn}:{self.version}"

    @property
    def artifact_filename(self) -> str:
        """Filename for the artifact: namespace-name-version.tar.gz"""
        return f"{self.namespace}-{self.name}-{self.version}.tar.gz"

    @classmethod
    def from_api(cls, data: dict) -> "CollectionVersion":
        """Create from API response."""
        return cls(
            namespace=data["namespace"],
            name=data["name"],
            version=data["version"],
            artifact_url=data.get("download_url"),
            artifact_sha256=data.get("sha256"),
            artifact_size=data.get("size"),
            dependencies=data.get("dependencies", {}),
            metadata=data,
            manifest=data.get("manifest", {}),
            files=data.get("files", {}),
            source_id=data.get("pulp_href") or data.get("id"),
        )


@dataclass
class Repository:
    """Ansible Collection Repository.

    A repository is a container for collections (e.g., rh-certified, community).
    """

    name: str
    description: Optional[str] = None
    pulp_href: Optional[str] = None

    # Configuration
    retain_repo_versions: Optional[int] = None
    remote: Optional[str] = None  # Link to remote if synced

    # Content
    latest_version_href: Optional[str] = None
    content_count: int = 0

    # Source/Target tracking
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "Repository":
        """Create from API response."""
        return cls(
            name=data["name"],
            description=data.get("description"),
            pulp_href=data.get("pulp_href"),
            retain_repo_versions=data.get("retain_repo_versions"),
            remote=data.get("remote"),
            latest_version_href=data.get("latest_version_href"),
            metadata=data,
            source_id=data.get("pulp_href"),
        )

    def to_dict(self) -> dict:
        """Convert to API format for creation."""
        return {
            "name": self.name,
            "description": self.description,
            "retain_repo_versions": self.retain_repo_versions,
            "remote": self.remote,
        }


@dataclass
class RemoteRegistry:
    """Remote collection source.

    Configures syncing from external sources (e.g., console.redhat.com, galaxy.ansible.com).
    """

    name: str
    url: str

    # Authentication
    auth_url: Optional[str] = None
    token: Optional[str] = None  # Should be encrypted via Vault
    username: Optional[str] = None
    password: Optional[str] = None  # Should be encrypted via Vault

    # Sync configuration
    requirements_file: Optional[str] = None
    sync_dependencies: bool = True
    download_concurrency: int = 10
    rate_limit: Optional[int] = None

    # TLS
    tls_validation: bool = True
    ca_cert: Optional[str] = None
    client_cert: Optional[str] = None
    client_key: Optional[str] = None  # Should be encrypted via Vault

    # Proxy
    proxy_url: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None  # Should be encrypted via Vault

    # Pulp
    pulp_href: Optional[str] = None

    # Tracking
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "RemoteRegistry":
        """Create from API response."""
        return cls(
            name=data["name"],
            url=data["url"],
            auth_url=data.get("auth_url"),
            requirements_file=data.get("requirements_file"),
            sync_dependencies=data.get("sync_dependencies", True),
            download_concurrency=data.get("download_concurrency", 10),
            rate_limit=data.get("rate_limit"),
            tls_validation=data.get("tls_validation", True),
            pulp_href=data.get("pulp_href"),
            metadata=data,
            source_id=data.get("pulp_href"),
        )

    def to_dict(self) -> dict:
        """Convert to API format for creation.

        Note: Credentials should be handled separately via Vault.
        """
        return {
            "name": self.name,
            "url": self.url,
            "auth_url": self.auth_url,
            "requirements_file": self.requirements_file,
            "sync_dependencies": self.sync_dependencies,
            "download_concurrency": self.download_concurrency,
            "rate_limit": self.rate_limit,
            "tls_validation": self.tls_validation,
            "proxy_url": self.proxy_url,
            # Credentials would be added here if provided
            # but should be encrypted
        }


@dataclass
class ExecutionEnvironment:
    """Execution Environment (Container Image).

    Represents a container image in Automation Hub's private registry.
    Examples: ee-minimal-rhel8, ee-supported-rhel8, custom-ee
    """

    name: str
    namespace: str
    description: Optional[str] = None

    # Container info
    container_repository_name: Optional[str] = None  # Full repo path
    pulp_href: Optional[str] = None

    # Metadata
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    tags_count: int = 0

    # Tracking
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    metadata: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        """Full name: namespace/name"""
        return f"{self.namespace}/{self.name}"

    @classmethod
    def from_api(cls, data: dict) -> "ExecutionEnvironment":
        """Create from API response."""
        # Extract namespace and name from pulp_container_repository_name
        repo_name = data.get("pulp_container_repository_name", "")
        parts = repo_name.split("/") if repo_name else []
        namespace = parts[0] if len(parts) > 0 else ""
        name = parts[1] if len(parts) > 1 else data.get("name", "")

        return cls(
            name=name,
            namespace=namespace,
            description=data.get("description"),
            container_repository_name=repo_name,
            pulp_href=data.get("pulp_href"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            tags_count=data.get("tags_count", 0),
            metadata=data,
            source_id=data.get("id") or data.get("pulp_href"),
        )

    def to_dict(self) -> dict:
        """Convert to API format for creation."""
        return {
            "name": self.container_repository_name or f"{self.namespace}/{self.name}",
            "description": self.description,
        }


@dataclass
class ContainerRepository:
    """Container Repository (for EE images).

    A Pulp repository that holds container image content.
    """

    name: str
    description: Optional[str] = None
    pulp_href: Optional[str] = None

    # Configuration
    retain_repo_versions: Optional[int] = None
    remote: Optional[str] = None  # Link to container remote

    # Content
    latest_version_href: Optional[str] = None

    # Tracking
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "ContainerRepository":
        """Create from API response."""
        return cls(
            name=data["name"],
            description=data.get("description"),
            pulp_href=data.get("pulp_href"),
            retain_repo_versions=data.get("retain_repo_versions"),
            remote=data.get("remote"),
            latest_version_href=data.get("latest_version_href"),
            metadata=data,
            source_id=data.get("pulp_href"),
        )

    def to_dict(self) -> dict:
        """Convert to API format for creation."""
        return {
            "name": self.name,
            "description": self.description,
            "retain_repo_versions": self.retain_repo_versions,
        }


@dataclass
class ContainerRemoteRegistry:
    """Remote container registry source.

    Configures syncing container images from external registries
    (e.g., registry.redhat.io, quay.io, docker.io).
    """

    name: str
    url: str

    # Authentication
    username: Optional[str] = None
    password: Optional[str] = None  # Should be encrypted via Vault

    # Sync configuration
    include_tags: Optional[list[str]] = None
    exclude_tags: Optional[list[str]] = None
    download_concurrency: int = 10
    rate_limit: Optional[int] = None

    # TLS
    tls_validation: bool = True
    ca_cert: Optional[str] = None
    client_cert: Optional[str] = None
    client_key: Optional[str] = None  # Should be encrypted via Vault

    # Proxy
    proxy_url: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None  # Should be encrypted via Vault

    # Pulp
    pulp_href: Optional[str] = None
    upstream_name: Optional[str] = None  # Upstream repository name

    # Tracking
    source_id: Optional[str] = None
    target_id: Optional[str] = None

    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "ContainerRemoteRegistry":
        """Create from API response."""
        return cls(
            name=data["name"],
            url=data["url"],
            upstream_name=data.get("upstream_name"),
            include_tags=data.get("include_tags"),
            exclude_tags=data.get("exclude_tags"),
            download_concurrency=data.get("download_concurrency", 10),
            rate_limit=data.get("rate_limit"),
            tls_validation=data.get("tls_validation", True),
            pulp_href=data.get("pulp_href"),
            metadata=data,
            source_id=data.get("pulp_href"),
        )

    def to_dict(self) -> dict:
        """Convert to API format for creation."""
        return {
            "name": self.name,
            "url": self.url,
            "upstream_name": self.upstream_name,
            "include_tags": self.include_tags,
            "exclude_tags": self.exclude_tags,
            "download_concurrency": self.download_concurrency,
            "rate_limit": self.rate_limit,
            "tls_validation": self.tls_validation,
            "proxy_url": self.proxy_url,
        }
