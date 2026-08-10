from collections.abc import Callable
from typing import Any

from aap_migration.client.exceptions import APIError, ConflictError
from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class UserImporter(ResourceImporter):
    """Importer for user resources."""

    DEPENDENCIES: dict[str, str] = {}  # No dependencies - users can exist independently
    IDENTIFIER_FIELD = "username"  # Users use 'username' instead of 'name'

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import a single user with password handling.

        Overrides parent to:
        1. Use 'username' field instead of 'name' for source_name tracking
        2. Add temporary password for all users (including superusers)
        3. Track users needing password reset

        Note: AAP requires a password but we cannot extract it from the source API,
        so all users (including superusers) get temporary passwords that must be reset.
        """
        # Check if already imported
        if self.state.is_migrated(resource_type, source_id):
            logger.debug(
                "resource_already_imported",
                resource_type=resource_type,
                source_id=source_id,
            )
            self.stats["skipped_count"] += 1
            return None

        # Mark as in progress with correct username field (users don't have 'name')
        self.state.mark_in_progress(
            resource_type=resource_type,
            source_id=source_id,
            source_name=data.get("username", "unknown"),
            phase="import",
        )

        try:
            # Remove password-related fields (cannot be migrated)
            data.pop("password", None)
            data.pop("ldap_dn", None)

            # Generate temporary password for all users (including superusers)
            # This is required because AAP API requires a password for new users
            # Use cached password from config for performance (same value for all users)
            temp_password = self.performance_config.get_dummy_password()
            data["password"] = temp_password

            logger.info(
                "user_temporary_password_set",
                username=data.get("username"),
                source_id=source_id,
                is_superuser=data.get("is_superuser", False),
            )

            # Resolve dependencies (users have none, but kept for consistency)
            if resolve_dependencies:
                data = await self._resolve_dependencies(resource_type, data)

            # Create resource
            result = await self.client.create_resource(
                resource_type=resource_type,
                data=data,
                check_exists=True,
            )

            # Mark as completed
            self.state.mark_completed(
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
                target_name=result.get("username"),
            )

            self.stats["imported_count"] += 1

            logger.info(
                "resource_imported",
                resource_type=resource_type,
                source_id=source_id,
                target_id=result["id"],
            )

            return result

        except ConflictError:
            # Handle conflict (user already exists via 409)
            conflict_result = await self._handle_user_conflict(source_id, data)
            if conflict_result:
                self.stats["conflict_count"] += 1
            return conflict_result

        except APIError as e:
            # AAP often returns 400 with "already exists" for duplicate usernames
            error_str = str(e).lower()
            is_already_exists = "already exists" in error_str or (
                e.response
                and any(
                    "already exists" in str(v).lower()
                    for v in (e.response.values() if isinstance(e.response, dict) else [])
                )
            )
            if is_already_exists:
                logger.warning(
                    "user_already_exists",
                    source_id=source_id,
                    username=data.get("username"),
                    error=str(e),
                )
                conflict_result = await self._handle_user_conflict(source_id, data)
                if conflict_result:
                    self.stats["conflict_count"] += 1
                    return conflict_result

            error_msg = str(e)
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_msg,
            )
            self.stats["error_count"] += 1
            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("username", "unknown"),
                    "error": error_msg,
                    "error_type": type(e).__name__,
                }
            )
            logger.error(
                "resource_import_failed",
                resource_type=resource_type,
                source_id=source_id,
                error=error_msg,
            )
            return None

        except Exception as e:
            # Mark as failed
            error_msg = str(e)
            self.state.mark_failed(
                resource_type=resource_type,
                source_id=source_id,
                error_message=error_msg,
            )
            self.stats["error_count"] += 1

            self.import_errors.append(
                {
                    "resource_type": resource_type,
                    "source_id": source_id,
                    "name": data.get("username", "unknown"),
                    "error": error_msg,
                }
            )

            logger.error(
                "resource_import_failed",
                resource_type=resource_type,
                source_id=source_id,
                error=error_msg,
            )

            return None

    async def _handle_user_conflict(
        self, source_id: int, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Map an existing target user by username when create conflicts."""
        username = data.get("username")
        if not username:
            return None
        try:
            results = await self.client.get("users/", params={"username": username})
            users = results.get("results") or []
            if not users:
                return None
            existing = users[0]
            target_id = int(existing["id"])
            self.state.mark_completed(
                resource_type="users",
                source_id=source_id,
                target_id=target_id,
                target_name=existing.get("username", username),
                source_name=username,
            )
            logger.info(
                "user_conflict_mapped",
                source_id=source_id,
                target_id=target_id,
                username=username,
            )
            return dict(existing)
        except Exception as exc:
            logger.error(
                "user_conflict_resolution_failed",
                source_id=source_id,
                username=username,
                error=str(exc),
            )
            self.state.mark_failed(
                resource_type="users",
                source_id=source_id,
                error_message=f"Conflict resolution failed: {exc}",
            )
            return None

    async def import_users(
        self,
        users: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple users concurrently with live progress updates.

        Uses higher concurrency than other resources since users have no
        dependencies, allowing faster import throughput.

        Note: Passwords cannot be migrated - users get temporary passwords.

        Args:
            users: List of user data
            progress_callback: Optional callback for progress updates.
                Called after each user with (success_count, failed_count).

        Returns:
            List of created user data
        """
        return await self._import_parallel(
            "users",
            users,
            progress_callback,
            concurrency=self.performance_config.user_import_max_concurrent,
        )


class TeamImporter(ResourceImporter):
    """Importer for team resources."""

    DEPENDENCIES: dict[str, str] = {
        "organization": "organizations",
    }

    async def import_teams(
        self,
        teams: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import multiple teams concurrently with live progress updates.

        Args:
            teams: List of team data
            progress_callback: Optional callback for progress updates.
                Called after each team with (success_count, failed_count).

        Returns:
            List of created team data
        """
        return await self._import_parallel("teams", teams, progress_callback)
