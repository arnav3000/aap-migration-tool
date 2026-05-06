"""
Automation Hub Exporter.

Exports namespaces, collections, repositories, and remotes from source AAP 2.4.
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

from aap_migration.automation_hub.client import GalaxyAPIClient
from aap_migration.automation_hub.exceptions import AutomationHubError, GalaxyAPIError
from aap_migration.automation_hub.models import (
    Namespace,
    CollectionVersion,
    Repository,
    RemoteRegistry,
    ExecutionEnvironment,
    ContainerRepository,
    ContainerRemoteRegistry,
)
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class AutomationHubExporter:
    """Exports Automation Hub content from source AAP 2.4."""

    def __init__(
        self,
        source_url: str,
        export_dir: Path,
        source_token: Optional[str] = None,
        source_username: Optional[str] = None,
        source_password: Optional[str] = None,
        verify_ssl: bool = True,
        download_artifacts: bool = True,
    ):
        """Initialize Automation Hub exporter.

        Args:
            source_url: Source Automation Hub URL
            export_dir: Directory to save exports
            source_token: Authentication token (AAP 2.6)
            source_username: Username for basic auth (AAP 2.4)
            source_password: Password for basic auth (AAP 2.4)
            verify_ssl: Whether to verify SSL certificates
            download_artifacts: Whether to download collection artifacts
        """
        self.source_url = source_url
        self.source_token = source_token
        self.source_username = source_username
        self.source_password = source_password
        self.export_dir = export_dir
        self.verify_ssl = verify_ssl
        self.download_artifacts = download_artifacts

        # Create export directory structure
        self.hub_dir = export_dir / "automation_hub"
        self.namespaces_dir = self.hub_dir / "namespaces"
        self.collections_dir = self.hub_dir / "collections"
        self.artifacts_dir = self.hub_dir / "artifacts"
        self.repositories_dir = self.hub_dir / "repositories"
        self.remotes_dir = self.hub_dir / "remotes"
        self.execution_environments_dir = self.hub_dir / "execution_environments"
        self.container_repositories_dir = self.hub_dir / "container_repositories"
        self.container_remotes_dir = self.hub_dir / "container_remotes"

        for d in [
            self.namespaces_dir,
            self.collections_dir,
            self.artifacts_dir,
            self.repositories_dir,
            self.remotes_dir,
            self.execution_environments_dir,
            self.container_repositories_dir,
            self.container_remotes_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

        self.client: Optional[GalaxyAPIClient] = None

    async def connect(self):
        """Connect to source Automation Hub."""
        self.client = GalaxyAPIClient(
            base_url=self.source_url,
            token=self.source_token,
            username=self.source_username,
            password=self.source_password,
            verify_ssl=self.verify_ssl,
        )
        await self.client.connect()
        logger.info("connected_to_source_hub", url=self.source_url)

    async def close(self):
        """Close connection to source."""
        if self.client:
            await self.client.close()
            self.client = None

    async def export_all(self):
        """Export all Automation Hub content.

        Exports in order:
        1. Namespaces
        2. Collections (versions)
        3. Artifacts (if download_artifacts=True)
        4. Repositories
        5. Remotes
        """
        logger.info("starting_hub_export", export_dir=str(self.hub_dir))

        try:
            await self.connect()

            # Export namespaces first (collections depend on them)
            namespaces = await self.export_namespaces()
            logger.info("exported_namespaces", count=len(namespaces))

            # Export collections
            collections = await self.export_collections()
            logger.info("exported_collections", count=len(collections))

            # Download artifacts if requested
            if self.download_artifacts:
                downloaded = await self.download_all_artifacts(collections)
                logger.info("downloaded_artifacts", count=downloaded)

            # Export repositories
            repositories = await self.export_repositories()
            logger.info("exported_repositories", count=len(repositories))

            # Export remotes
            remotes = await self.export_remotes()
            logger.info("exported_remotes", count=len(remotes))

            # Export container infrastructure
            container_repos = await self.export_container_repositories()
            logger.info("exported_container_repositories", count=len(container_repos))

            container_remotes = await self.export_container_remotes()
            logger.info("exported_container_remotes", count=len(container_remotes))

            # Export execution environments
            ees = await self.export_execution_environments()
            logger.info("exported_execution_environments", count=len(ees))

            # Write summary
            await self._write_summary(
                namespaces=len(namespaces),
                collections=len(collections),
                repositories=len(repositories),
                remotes=len(remotes),
                container_repositories=len(container_repos),
                container_remotes=len(container_remotes),
                execution_environments=len(ees),
            )

            logger.info(
                "hub_export_completed",
                namespaces=len(namespaces),
                collections=len(collections),
                repositories=len(repositories),
                remotes=len(remotes),
                container_repositories=len(container_repos),
                container_remotes=len(container_remotes),
                execution_environments=len(ees),
            )

        finally:
            await self.close()

    async def export_namespaces(self) -> list[Namespace]:
        """Export all namespaces.

        Returns:
            List of exported namespaces
        """
        logger.info("exporting_namespaces")

        namespaces = await self.client.list_namespaces()

        # Save each namespace
        for ns in namespaces:
            output_file = self.namespaces_dir / f"{ns.name}.json"
            with open(output_file, "w") as f:
                json.dump(ns.metadata, f, indent=2)

            logger.debug("namespace_exported", name=ns.name, file=str(output_file))

        # Save namespaces index
        index_file = self.namespaces_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(namespaces),
                    "namespaces": [
                        {
                            "name": ns.name,
                            "company": ns.company,
                            "description": ns.description,
                        }
                        for ns in namespaces
                    ],
                },
                f,
                indent=2,
            )

        logger.info("namespaces_exported", count=len(namespaces))
        return namespaces

    async def export_collections(self) -> list[CollectionVersion]:
        """Export all collection versions.

        Returns:
            List of exported collection versions
        """
        logger.info("exporting_collections")

        # Get all collection versions
        versions = await self.client.list_collections()

        # Group by collection for better organization
        collections_map = {}
        for v in versions:
            key = f"{v.namespace}.{v.name}"
            if key not in collections_map:
                collections_map[key] = []
            collections_map[key].append(v)

        # Save each collection's versions
        for fqn, collection_versions in collections_map.items():
            namespace, name = fqn.split(".")

            # Create namespace directory
            ns_dir = self.collections_dir / namespace
            ns_dir.mkdir(exist_ok=True)

            # Save collection versions
            output_file = ns_dir / f"{name}.json"
            with open(output_file, "w") as f:
                json.dump(
                    {
                        "namespace": namespace,
                        "name": name,
                        "versions": [v.metadata for v in collection_versions],
                    },
                    f,
                    indent=2,
                )

            logger.debug(
                "collection_exported",
                fqn=fqn,
                versions=len(collection_versions),
                file=str(output_file),
            )

        # Save collections index
        index_file = self.collections_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(versions),
                    "collections": len(collections_map),
                    "items": [
                        {
                            "fqn": fqn,
                            "namespace": fqn.split(".")[0],
                            "name": fqn.split(".")[1],
                            "version_count": len(vers),
                            "versions": [v.version for v in vers],
                        }
                        for fqn, vers in sorted(collections_map.items())
                    ],
                },
                f,
                indent=2,
            )

        logger.info(
            "collections_exported",
            total_versions=len(versions),
            unique_collections=len(collections_map),
        )
        return versions

    async def download_all_artifacts(
        self, collections: list[CollectionVersion]
    ) -> int:
        """Download all collection artifacts.

        Args:
            collections: List of collection versions to download

        Returns:
            Number of artifacts successfully downloaded
        """
        logger.info("downloading_artifacts", total=len(collections))

        downloaded = 0
        failed = 0

        for version in collections:
            if not version.artifact_url:
                logger.warning(
                    "no_artifact_url",
                    fqn=version.fqn,
                    version=version.version,
                )
                continue

            try:
                # Create namespace directory
                ns_dir = self.artifacts_dir / version.namespace
                ns_dir.mkdir(exist_ok=True)

                # Download artifact
                output_path = ns_dir / version.artifact_filename
                if output_path.exists():
                    logger.debug(
                        "artifact_already_exists",
                        file=str(output_path),
                    )
                    downloaded += 1
                    continue

                await self.client.download_collection_artifact(
                    download_url=version.artifact_url,
                    output_path=output_path,
                )

                downloaded += 1
                logger.debug(
                    "artifact_downloaded",
                    fqn=version.fqn,
                    version=version.version,
                    file=str(output_path),
                )

            except Exception as e:
                failed += 1
                logger.error(
                    "artifact_download_failed",
                    fqn=version.fqn,
                    version=version.version,
                    error=str(e),
                )

        logger.info(
            "artifacts_download_completed",
            downloaded=downloaded,
            failed=failed,
            total=len(collections),
        )
        return downloaded

    async def export_repositories(self) -> list[Repository]:
        """Export all repositories.

        Returns:
            List of exported repositories
        """
        logger.info("exporting_repositories")

        repositories = await self.client.list_repositories()

        # Save each repository
        for repo in repositories:
            # Sanitize name for filename
            filename = repo.name.replace("/", "_").replace(" ", "_")
            output_file = self.repositories_dir / f"{filename}.json"

            with open(output_file, "w") as f:
                json.dump(repo.metadata, f, indent=2)

            logger.debug("repository_exported", name=repo.name, file=str(output_file))

        # Save repositories index
        index_file = self.repositories_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(repositories),
                    "repositories": [
                        {
                            "name": repo.name,
                            "description": repo.description,
                            "pulp_href": repo.pulp_href,
                        }
                        for repo in repositories
                    ],
                },
                f,
                indent=2,
            )

        logger.info("repositories_exported", count=len(repositories))
        return repositories

    async def export_remotes(self) -> list[RemoteRegistry]:
        """Export all remote registries.

        Returns:
            List of exported remotes
        """
        logger.info("exporting_remotes")

        remotes = await self.client.list_remotes()

        # Save each remote
        for remote in remotes:
            # Sanitize name for filename
            filename = remote.name.replace("/", "_").replace(" ", "_")
            output_file = self.remotes_dir / f"{filename}.json"

            with open(output_file, "w") as f:
                json.dump(remote.metadata, f, indent=2)

            logger.debug("remote_exported", name=remote.name, file=str(output_file))

        # Save remotes index
        index_file = self.remotes_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(remotes),
                    "remotes": [
                        {
                            "name": remote.name,
                            "url": remote.url,
                            "pulp_href": remote.pulp_href,
                        }
                        for remote in remotes
                    ],
                },
                f,
                indent=2,
            )

        logger.info("remotes_exported", count=len(remotes))
        return remotes

    async def export_container_repositories(self) -> list[ContainerRepository]:
        """Export all container repositories.

        Returns:
            List of exported container repositories
        """
        logger.info("exporting_container_repositories")

        try:
            container_repos = await self.client.list_container_repositories()
        except GalaxyAPIError as e:
            if e.status_code == 404:
                logger.warning(
                    "container_repositories_not_supported",
                    message="Container repositories not available (AAP 2.4.x doesn't support EE)",
                )
                return []
            raise

        # Save each container repository
        for repo in container_repos:
            # Sanitize name for filename
            filename = repo.name.replace("/", "_").replace(" ", "_")
            output_file = self.container_repositories_dir / f"{filename}.json"

            with open(output_file, "w") as f:
                json.dump(repo.metadata, f, indent=2)

            logger.debug(
                "container_repository_exported", name=repo.name, file=str(output_file)
            )

        # Save container repositories index
        index_file = self.container_repositories_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(container_repos),
                    "container_repositories": [
                        {
                            "name": repo.name,
                            "description": repo.description,
                            "pulp_href": repo.pulp_href,
                        }
                        for repo in container_repos
                    ],
                },
                f,
                indent=2,
            )

        logger.info("container_repositories_exported", count=len(container_repos))
        return container_repos

    async def export_container_remotes(self) -> list[ContainerRemoteRegistry]:
        """Export all container remote registries.

        Returns:
            List of exported container remotes
        """
        logger.info("exporting_container_remotes")

        try:
            container_remotes = await self.client.list_container_remotes()
        except GalaxyAPIError as e:
            if e.status_code == 404:
                logger.warning(
                    "container_remotes_not_supported",
                    message="Container remotes not available (AAP 2.4.x doesn't support EE)",
                )
                return []
            raise

        # Save each container remote
        for remote in container_remotes:
            # Sanitize name for filename
            filename = remote.name.replace("/", "_").replace(" ", "_")
            output_file = self.container_remotes_dir / f"{filename}.json"

            with open(output_file, "w") as f:
                json.dump(remote.metadata, f, indent=2)

            logger.debug(
                "container_remote_exported", name=remote.name, file=str(output_file)
            )

        # Save container remotes index
        index_file = self.container_remotes_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(container_remotes),
                    "container_remotes": [
                        {
                            "name": remote.name,
                            "url": remote.url,
                            "pulp_href": remote.pulp_href,
                        }
                        for remote in container_remotes
                    ],
                },
                f,
                indent=2,
            )

        logger.info("container_remotes_exported", count=len(container_remotes))
        return container_remotes

    async def export_execution_environments(self) -> list[ExecutionEnvironment]:
        """Export all execution environments.

        Returns:
            List of exported execution environments
        """
        logger.info("exporting_execution_environments")

        try:
            ees = await self.client.list_execution_environments()
        except GalaxyAPIError as e:
            if e.status_code == 404:
                logger.warning(
                    "execution_environments_not_supported",
                    message="Execution environments not available (AAP 2.4.x doesn't support EE)",
                )
                return []
            raise

        # Save each execution environment
        for ee in ees:
            # Sanitize name for filename
            filename = ee.full_name.replace("/", "_").replace(" ", "_")
            output_file = self.execution_environments_dir / f"{filename}.json"

            with open(output_file, "w") as f:
                json.dump(ee.metadata, f, indent=2)

            logger.debug(
                "execution_environment_exported", name=ee.full_name, file=str(output_file)
            )

        # Save execution environments index
        index_file = self.execution_environments_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(
                {
                    "count": len(ees),
                    "execution_environments": [
                        {
                            "name": ee.full_name,
                            "description": ee.description,
                            "pulp_href": ee.pulp_href,
                            "tags_count": ee.tags_count,
                        }
                        for ee in ees
                    ],
                },
                f,
                indent=2,
            )

        logger.info("execution_environments_exported", count=len(ees))
        return ees

    async def _write_summary(
        self,
        namespaces: int,
        collections: int,
        repositories: int,
        remotes: int,
        container_repositories: int,
        container_remotes: int,
        execution_environments: int,
    ):
        """Write export summary file.

        Args:
            namespaces: Number of namespaces exported
            collections: Number of collections exported
            repositories: Number of repositories exported
            remotes: Number of remotes exported
            container_repositories: Number of container repositories exported
            container_remotes: Number of container remotes exported
            execution_environments: Number of execution environments exported
        """
        summary_file = self.hub_dir / "export_summary.json"

        summary = {
            "source_url": self.source_url,
            "export_dir": str(self.hub_dir),
            "artifacts_downloaded": self.download_artifacts,
            "counts": {
                "namespaces": namespaces,
                "collections": collections,
                "repositories": repositories,
                "remotes": remotes,
                "container_repositories": container_repositories,
                "container_remotes": container_remotes,
                "execution_environments": execution_environments,
            },
        }

        with open(summary_file, "w") as f:
            json.dump(summary, f, indent=2)

        logger.info("export_summary_written", file=str(summary_file))
