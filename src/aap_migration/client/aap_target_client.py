"""AAP 2.5+ Target Client for importing resources.

This client provides methods to create resources in AAP 2.5+ with
Platform Gateway support (required for AAP 2.6+) and bulk operations.
"""

from typing import Any, cast

from aap_migration.client.base_client import BaseAPIClient
from aap_migration.client.exceptions import ConflictError
from aap_migration.config import AAPInstanceConfig
from aap_migration.resources import get_endpoint
from aap_migration.utils.logging import get_logger
from aap_migration.utils.retry import retry_api_call

logger = get_logger(__name__)


class AAPTargetClient(BaseAPIClient):
    """Client for AAP 2.5+ target instance.

    This client extends BaseAPIClient with AAP-specific methods for
    creating resources, including bulk operations support. It handles
    the Platform Gateway routing automatically for AAP 2.6+.

    Supports AAP versions 2.5, 2.6, and later.
    """

    def __init__(
        self,
        config: AAPInstanceConfig,
        rate_limit: int = 20,
        log_payloads: bool = False,
        max_payload_size: int = 10000,
        max_connections: int | None = None,
        max_keepalive_connections: int | None = None,
        auth_scheme: str = "Bearer",
    ):
        """Initialize AAP target client.

        Args:
            config: AAP instance configuration (should include /api/controller/v2/ path)
            rate_limit: Maximum requests per second
            log_payloads: Enable request/response payload logging
            max_payload_size: Maximum payload size to log before truncation
            max_connections: Maximum number of connections in pool
            max_keepalive_connections: Maximum keep-alive connections
            auth_scheme: Authorization header scheme (Bearer or Token)
        """
        # Ensure URL includes Platform Gateway path
        base_url = config.url
        if not base_url.endswith("/api/controller/v2"):
            if "/api/controller/v2" in base_url:
                # Already has the path somewhere
                pass
            else:
                # Append Platform Gateway path
                base_url = f"{base_url.rstrip('/')}/api/controller/v2"

        super().__init__(
            base_url=base_url,
            token=config.token,
            verify_ssl=config.verify_ssl,
            timeout=config.timeout,
            rate_limit=rate_limit,
            log_payloads=log_payloads,
            max_payload_size=max_payload_size,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            auth_scheme=auth_scheme,
        )
        logger.info("aap_target_client_initialized", url=base_url)
        self._version_cache: str | None = None

    @retry_api_call
    async def get_version(self) -> str:
        """Detect the AAP version from the /config/ endpoint.

        Returns:
            Version string (e.g., "2.5.0", "2.6.1")

        Raises:
            APIError: If version cannot be determined
        """
        if self._version_cache:
            return self._version_cache

        try:
            config_data = await self.get("config/")
            version = config_data.get("version", "")

            if not version:
                logger.warning(
                    "version_not_found_in_config",
                    message="Version field not found in /config/ response, using fallback",
                )
                # Try to extract from ansible_version or default to 2.6.0
                version = config_data.get("ansible_version", "2.6.0")

            self._version_cache = version
            logger.info("aap_version_detected", version=version, url=self.base_url)
            return str(version)

        except Exception as e:
            logger.error(
                "version_detection_failed",
                error=str(e),
                message="Failed to detect AAP version, defaulting to 2.6.0",
            )
            # Default to 2.6.0 if detection fails
            self._version_cache = "2.6.0"
            return self._version_cache

    # Core CRUD operations
    @retry_api_call
    async def create_resource(
        self,
        resource_type: str,
        data: dict[str, Any],
        check_exists: bool = True,
    ) -> dict[str, Any]:
        """Create a resource in AAP 2.6.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')
            data: Resource data
            check_exists: Check if resource exists before creating (idempotency)

        Returns:
            Created resource data

        Raises:
            ConflictError: If resource already exists (409)
        """
        endpoint = f"{resource_type}/"

        try:
            result = await self.post(endpoint, json_data=data)
            logger.info(
                "resource_created",
                resource_type=resource_type,
                resource_id=result.get("id"),
                resource_name=result.get("name"),
            )
            return result
        except ConflictError:
            if check_exists:
                logger.info(
                    "resource_already_exists",
                    resource_type=resource_type,
                    resource_name=data.get("name"),
                )
                # Try to find the existing resource
                existing = await self.find_resource_by_name(resource_type, data.get("name", ""))
                if existing:
                    return existing
            raise

    @retry_api_call
    async def update_resource(
        self,
        resource_type: str,
        resource_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a resource in AAP 2.6.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')
            resource_id: Resource ID
            data: Resource data to update

        Returns:
            Updated resource data
        """
        base = get_endpoint(resource_type).rstrip("/")
        endpoint = f"{base}/{resource_id}/"
        result = await self.patch(endpoint, json_data=data)
        logger.info(
            "resource_updated",
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return result

    @retry_api_call
    async def delete_resource(
        self,
        resource_type: str,
        resource_id: int,
    ) -> dict[str, Any]:
        """Delete a resource from AAP 2.6.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')
            resource_id: Resource ID

        Returns:
            Empty dict or deletion confirmation
        """
        base = get_endpoint(resource_type).rstrip("/")
        endpoint = f"{base}/{resource_id}/"
        result = await self.delete(endpoint)
        logger.info(
            "resource_deleted",
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return result

    @retry_api_call
    async def get_resource(
        self,
        resource_type: str,
        resource_id: int,
    ) -> dict[str, Any]:
        """Get a resource from AAP 2.6.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')
            resource_id: Resource ID

        Returns:
            Resource data
        """
        endpoint = f"{resource_type}/{resource_id}/"
        return await self.get(endpoint)

    @retry_api_call
    async def resource_exists(
        self,
        resource_type: str,
        resource_id: int,
    ) -> bool:
        """Check if a resource exists.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')
            resource_id: Resource ID

        Returns:
            True if resource exists, False otherwise
        """
        try:
            await self.get_resource(resource_type, resource_id)
            return True
        except Exception:
            return False

    @retry_api_call
    async def find_resource_by_name(
        self,
        resource_type: str,
        name: str,
        organization: str | None = None,
        organization_id: int | None = None,
        parent_id: int | None = None,
        parent_field: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a resource by name.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')
            name: Resource name
            organization: Optional organization name to filter by
            organization_id: Optional organization ID to filter by (takes precedence over organization)
            parent_id: Optional parent resource ID (for parent-scoped resources like inventory_sources)
            parent_field: Parent field name (e.g., 'inventory', 'unified_job_template')

        Returns:
            Resource data if found, None otherwise
        """
        params: dict[str, Any] = {"name": name, "page_size": 1}

        # Filter by organization ID (preferred) or organization name
        if organization_id:
            params["organization"] = organization_id
        elif organization:
            params["organization__name"] = organization

        # Filter by parent field (for parent-scoped resources)
        if parent_id is not None and parent_field:
            params[parent_field] = parent_id

        endpoint = f"{resource_type}/"
        response = await self.get(endpoint, params=params)

        results = response.get("results", [])
        return results[0] if results else None

    # Specific resource creation methods
    @retry_api_call
    async def create_organization(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create an organization.

        Args:
            data: Organization data

        Returns:
            Created organization
        """
        return await self.create_resource("organizations", data)

    @retry_api_call
    async def create_inventory(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create an inventory.

        Args:
            data: Inventory data

        Returns:
            Created inventory
        """
        return await self.create_resource("inventories", data)

    @retry_api_call
    async def create_host(self, inventory_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """Create a single host.

        For creating multiple hosts, use bulk_create_hosts instead.

        Args:
            inventory_id: Inventory ID
            data: Host data

        Returns:
            Created host
        """
        data["inventory"] = inventory_id
        return await self.create_resource("hosts", data)

    @retry_api_call
    async def create_credential(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a credential.

        Args:
            data: Credential data

        Returns:
            Created credential
        """
        return await self.create_resource("credentials", data)

    @retry_api_call
    async def create_project(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a project.

        Args:
            data: Project data

        Returns:
            Created project
        """
        return await self.create_resource("projects", data)

    @retry_api_call
    async def create_job_template(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a job template.

        Args:
            data: Job template data

        Returns:
            Created job template
        """
        return await self.create_resource("job_templates", data)

    @retry_api_call
    async def create_workflow_job_template(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a workflow job template.

        Args:
            data: Workflow job template data

        Returns:
            Created workflow job template
        """
        return await self.create_resource("workflow_job_templates", data)

    # Job operations
    @retry_api_call
    async def launch_job_template(
        self,
        job_template_id: int,
        extra_vars: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Launch a job template.

        Args:
            job_template_id: Job template ID
            extra_vars: Optional extra variables

        Returns:
            Job data
        """
        endpoint = f"job_templates/{job_template_id}/launch/"
        data = {"extra_vars": extra_vars} if extra_vars else {}
        return await self.post(endpoint, json_data=data)

    @retry_api_call
    async def get_job_status(self, job_id: int) -> dict[str, Any]:
        """Get job status.

        Args:
            job_id: Job ID

        Returns:
            Job data with status
        """
        endpoint = f"jobs/{job_id}/"
        return await self.get(endpoint)

    @retry_api_call
    async def cancel_job(self, job_id: int, endpoint_prefix: str = "jobs") -> dict[str, Any]:
        """Cancel a running job (AWX-inspired implementation).

        This method implements AWX best practices:
        1. Check current job status first
        2. Verify job is not already finished
        3. Check if job can be cancelled (can_cancel field)
        4. Handle race conditions gracefully

        Args:
            job_id: Job ID to cancel
            endpoint_prefix: Job endpoint (e.g., "jobs", "workflow_jobs", "project_updates")

        Returns:
            Updated job data after cancellation (or current status if already finished)

        Raises:
            APIError: If cancellation fails (excluding expected cases)

        Example:
            >>> job = await client.cancel_job(12345, "workflow_jobs")
            >>> print(job["status"])  # "canceling" or "canceled"
        """
        from aap_migration.resources import JOB_TERMINAL_STATUSES, JOB_TRANSIENT_STATUSES
        from aap_migration.utils.logging import get_logger

        logger = get_logger(__name__)

        # 1. Get current job status first
        job_endpoint = f"{endpoint_prefix}/{job_id}/"
        try:
            job = await self.get(job_endpoint)
        except Exception as e:
            logger.error(
                "failed_to_get_job_status", job_id=job_id, endpoint=endpoint_prefix, error=str(e)
            )
            raise

        current_status = job.get("status", "unknown")

        # 2. Check if job is already finished or canceling
        if current_status in JOB_TERMINAL_STATUSES:
            logger.info(
                "job_already_finished",
                job_id=job_id,
                endpoint=endpoint_prefix,
                status=current_status,
            )
            return job

        if current_status in JOB_TRANSIENT_STATUSES:
            logger.info(
                "job_already_canceling",
                job_id=job_id,
                endpoint=endpoint_prefix,
                status=current_status,
            )
            return job

        # 3. Check if job can be cancelled (AWX pattern)
        can_cancel = job.get("can_cancel", True)  # Default True for backward compatibility
        if not can_cancel:
            logger.warning(
                "job_not_cancellable",
                job_id=job_id,
                endpoint=endpoint_prefix,
                status=current_status,
                can_cancel=can_cancel,
            )
            return job

        # 4. POST to cancel endpoint
        cancel_endpoint = f"{endpoint_prefix}/{job_id}/cancel/"
        try:
            result = await self.post(cancel_endpoint, json_data={})
            logger.info(
                "job_cancelled_successfully",
                job_id=job_id,
                endpoint=endpoint_prefix,
                previous_status=current_status,
            )
            return result
        except Exception as e:
            # Handle race condition - job may have finished between check and cancel
            if hasattr(e, "status_code") and e.status_code == 405:
                # Method not allowed - this job type doesn't support cancellation
                logger.debug(
                    "job_type_does_not_support_cancel",
                    job_id=job_id,
                    endpoint=endpoint_prefix,
                    status_code=405,
                )
                raise  # Let caller handle 405
            elif "not allowed" in str(e).lower():
                logger.info(
                    "job_finished_during_cancel_attempt",
                    job_id=job_id,
                    endpoint=endpoint_prefix,
                    error=str(e),
                )
                # Return refreshed status
                return await self.get(job_endpoint)
            else:
                # Unexpected error
                logger.error(
                    "job_cancel_failed",
                    job_id=job_id,
                    endpoint=endpoint_prefix,
                    error=str(e),
                    status_code=getattr(e, "status_code", None),
                )
                raise

    # Validation and count methods
    async def get_count(self, resource_type: str) -> int:
        """Get count of resources.

        Args:
            resource_type: Resource type (e.g., 'inventories', 'hosts')

        Returns:
            Count of resources
        """
        endpoint = f"{resource_type}/"
        response = await self.get(endpoint, params={"page_size": 1})
        return int(response.get("count", 0))

    @retry_api_call
    async def list_resources(
        self,
        resource_type: str,
        filters: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """List resources with optional filters and automatic pagination.

        Args:
            resource_type: Resource type (e.g., 'users', 'organizations')
            filters: Query filters (e.g., {"name__in": "user1,user2,user3"})
            page_size: Number of results per page (default: 100)

        Returns:
            List of all matching resources (all pages combined)

        Example:
            # Batch query users by username
            users = await client.list_resources(
                "users",
                filters={"username__in": "alice,bob,charlie"}
            )
        """
        # Use get_endpoint to map resource_type to correct API endpoint
        # (e.g., "inventory_groups" -> "groups/")
        endpoint: str | None = get_endpoint(resource_type)
        params: dict[str, Any] = {"page_size": page_size}

        if filters:
            params.update(filters)

        all_results: list[dict[str, Any]] = []
        max_pages = 500

        page = 0
        while endpoint and page < max_pages:
            page += 1
            response = await self.get(endpoint, params=params)
            results = response.get("results", [])
            all_results.extend(results)

            next_url = response.get("next")
            if next_url:
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(next_url)
                raw_path = parsed.path.replace("/api/controller/v2/", "").replace("/api/v2/", "")
                endpoint = raw_path
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            else:
                endpoint = None

        if page >= max_pages:
            logger.warning(
                "list_resources_page_limit",
                resource_type=resource_type,
                pages=page,
                results=len(all_results),
            )

        logger.debug(
            "list_resources_completed",
            resource_type=resource_type,
            total_results=len(all_results),
            filters=filters,
        )

        return all_results

    async def validate_connectivity(self) -> bool:
        """Validate connectivity to AAP 2.6 instance.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            await self.get("ping/")
            logger.info("aap_connectivity_validated")
            return True
        except Exception as e:
            logger.error("aap_connectivity_failed", error=str(e))
            return False

    # Gateway API methods (AAP 2.6+)
    @retry_api_call
    async def create_gateway_authenticator(
        self,
        name: str,
        plugin_type: str,
        configuration: dict[str, Any],
        enabled: bool = True,
        create_objects: bool = True,
        remove_users: bool = False,
        order: int = 2,
    ) -> dict[str, Any]:
        """Create a Platform Gateway authenticator (AAP 2.6+).

        This method creates authenticators via the Platform Gateway API, which is
        separate from the Controller API. Used for LDAP, SAML, OIDC, etc.

        Args:
            name: Authenticator name (e.g., "Primary LDAP")
            plugin_type: Plugin type (e.g., "ansible_base.authentication.authenticator_plugins.ldap")
            configuration: Authenticator configuration (field names without AUTH_LDAP_ prefix)
            enabled: Enable the authenticator
            create_objects: Auto-create users on login
            remove_users: Remove users when they no longer authenticate
            order: Authenticator order (1=local, 2+=external)

        Returns:
            Created authenticator data

        Example:
            >>> config = {
            ...     "SERVER_URI": ["ldap://ldap.example.com:389"],
            ...     "BIND_DN": "cn=svc,ou=ServiceAccounts,dc=example,dc=com",
            ...     "USER_SEARCH": ["ou=Users,dc=example,dc=com", "SCOPE_SUBTREE", "(uid=%(user)s)"]
            ... }
            >>> result = await client.create_gateway_authenticator(
            ...     name="Primary LDAP",
            ...     plugin_type="ansible_base.authentication.authenticator_plugins.ldap",
            ...     configuration=config
            ... )
        """
        # Gateway API is at /api/gateway/v1/ (different from controller API)
        # Need to construct the full URL, not relative to controller API
        gateway_url = self.base_url.replace("/api/controller/v2", "/api/gateway/v1")
        endpoint = f"{gateway_url}/authenticators/"

        payload = {
            "name": name,
            "type": plugin_type,
            "enabled": enabled,
            "create_objects": create_objects,
            "remove_users": remove_users,
            "order": order,
            "configuration": configuration,
        }

        try:
            # Use direct httpx client since endpoint is absolute URL
            response = await self.client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            result = response.json()

            logger.info(
                "gateway_authenticator_created",
                name=name,
                plugin_type=plugin_type,
                authenticator_id=result.get("id"),
            )
            return cast(dict[str, Any], result)

        except Exception as e:
            logger.error(
                "gateway_authenticator_creation_failed",
                name=name,
                plugin_type=plugin_type,
                error=str(e),
            )
            raise

    @retry_api_call
    async def list_gateway_authenticators(self) -> list[dict[str, Any]]:
        """List all Platform Gateway authenticators (AAP 2.6+).

        Returns:
            List of authenticators
        """
        gateway_url = self.base_url.replace("/api/controller/v2", "/api/gateway/v1")
        endpoint = f"{gateway_url}/authenticators/"

        try:
            response = await self.client.get(
                endpoint,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            data = response.json()
            return list(data.get("results", []))

        except Exception as e:
            logger.error("gateway_authenticators_list_failed", error=str(e))
            raise

    @retry_api_call
    async def create_authenticator_map(
        self,
        authenticator_id: int,
        name: str,
        map_type: str,
        triggers: dict[str, Any],
        organization: str | None = None,
        team: str | None = None,
        role: str | None = None,
        revoke: bool = False,
        order: int = 10,
    ) -> dict[str, Any]:
        """Create an authenticator map for organization/team/user mappings (AAP 2.6+).

        Args:
            authenticator_id: Authenticator ID to attach the map to
            name: Map name (e.g., "LDAP - Engineering - Members")
            map_type: Map type (organization, team, is_superuser, role, allow)
            triggers: Trigger conditions (e.g., {"groups": {"has_or": ["cn=..."]}})
            organization: Organization name (for organization/team map types)
            team: Team name (for team map type)
            role: Role name (e.g., "Organization Member", "Team Member")
            revoke: Whether to revoke permission if user doesn't match
            order: Processing order (lower = higher precedence)

        Returns:
            Created authenticator map data

        Example:
            >>> triggers = {"groups": {"has_or": ["cn=Engineering-Users,ou=Groups,dc=example,dc=com"]}}
            >>> result = await client.create_authenticator_map(
            ...     authenticator_id=2,
            ...     name="LDAP - Global Engineering - Members",
            ...     map_type="organization",
            ...     organization="Global Engineering",
            ...     role="Organization Member",
            ...     triggers=triggers
            ... )
        """
        gateway_url = self.base_url.replace("/api/controller/v2", "/api/gateway/v1")
        endpoint = f"{gateway_url}/authenticator_maps/"

        payload: dict[str, Any] = {
            "name": name,
            "authenticator": authenticator_id,
            "map_type": map_type,
            "triggers": triggers,
            "revoke": revoke,
            "order": order,
        }

        # Add optional fields based on map type
        if organization:
            payload["organization"] = organization
        if team:
            payload["team"] = team
        if role:
            payload["role"] = role

        try:
            response = await self.client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            result = response.json()

            logger.info(
                "authenticator_map_created",
                map_id=result.get("id"),
                name=name,
                map_type=map_type,
                authenticator_id=authenticator_id,
            )
            return cast(dict[str, Any], result)

        except Exception as e:
            logger.error(
                "authenticator_map_creation_failed",
                name=name,
                map_type=map_type,
                authenticator_id=authenticator_id,
                error=str(e),
            )
            raise
