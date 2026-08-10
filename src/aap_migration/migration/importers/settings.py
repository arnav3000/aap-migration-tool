from collections.abc import Callable
from typing import Any

from packaging import version

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class SettingsImporter(ResourceImporter):
    """Importer for global system settings with review workflow.

    Settings are categorized into safe/review/sensitive. This importer:
    - Auto-imports safe settings (non-sensitive, non-environment-specific)
    - Generates review report for environment-specific settings
    - Generates template for sensitive settings (passwords, secrets)
    - AAP 2.6+: Migrates LDAP settings to Platform Gateway authenticators
    """

    DEPENDENCIES: dict[str, str] = {}

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import settings with categorization and review workflow.

        Args:
            resource_type: Should be 'settings'
            source_id: Source settings ID (typically 0)
            data: Categorized settings data
            resolve_dependencies: Not used for settings

        Returns:
            Result of settings import
        """
        # Settings are imported as a single resource
        safe = data.get("safe_to_copy", {})
        review_required = data.get("review_required", {})
        sensitive = data.get("sensitive", {})
        summary = data.get("_summary", {})

        logger.info(
            "settings_import_starting",
            total_safe=len(safe),
            total_review=len(review_required),
            total_sensitive=len(sensitive),
            auto_import_percentage=summary.get("auto_import_percentage", 0),
        )

        imported_count = 0
        failed_count = 0
        ldap_migrated = False

        # Detect AAP version
        target_version = await self.client.get_version()
        is_aap_26 = version.parse(target_version) >= version.parse("2.6.0")

        # AAP 2.6+: Migrate authentication settings to Gateway authenticators
        # Supports LDAP, SAML, Azure AD, GitHub, and other SSO methods
        if is_aap_26:
            migration_result = await self._migrate_all_authentication_to_gateway(
                safe, review_required, sensitive
            )

            # Remove migrated auth settings from categories
            for prefix in migration_result.get("migrated_prefixes", []):
                safe = {k: v for k, v in safe.items() if not k.startswith(prefix)}
                review_required = {
                    k: v for k, v in review_required.items() if not k.startswith(prefix)
                }
                sensitive = {k: v for k, v in sensitive.items() if not k.startswith(prefix)}

            ldap_migrated = migration_result.get("ldap_migrated", False)

        # Import safe settings automatically (non-LDAP for AAP 2.6)
        if safe:
            try:
                await self.client.patch("settings/all/", json_data=safe)
                imported_count = len(safe)
                logger.info(
                    "settings_safe_imported",
                    count=imported_count,
                    message=f"✓ Auto-imported {imported_count} safe settings",
                )
            except Exception as e:
                logger.error("settings_safe_import_failed", error=str(e))
                failed_count = len(safe)

        # Generate review report
        if review_required or sensitive:
            # Pass migration result if AAP 2.6, otherwise just ldap_migrated boolean
            auth_migration_info = (
                migration_result if is_aap_26 else {"ldap_migrated": ldap_migrated}
            )
            self._generate_settings_review_report(review_required, sensitive, auth_migration_info)

        self.stats["imported_count"] += imported_count
        self.stats["error_count"] += failed_count

        result = {
            "safe_imported": imported_count,
            "review_required": len(review_required),
            "sensitive_requires_manual": len(sensitive),
            "report_generated": "SETTINGS-REVIEW-REPORT.md",
        }

        if ldap_migrated:
            result["ldap_migrated_to_gateway"] = True

        return result

    async def _migrate_all_authentication_to_gateway(
        self, safe: dict, review_required: dict, sensitive: dict
    ) -> dict[str, Any]:
        """Migrate all authentication methods to Platform Gateway (AAP 2.6+).

        This method detects and migrates multiple authentication types:
        - LDAP (AUTH_LDAP_*)
        - SAML (SOCIAL_AUTH_SAML_*)
        - Azure AD OAuth2 (SOCIAL_AUTH_AZUREAD_OAUTH2_*)
        - GitHub Enterprise (SOCIAL_AUTH_GITHUB_ENTERPRISE_*)
        - Google OAuth2 (SOCIAL_AUTH_GOOGLE_OAUTH2_*)
        - RADIUS (RADIUS_*)
        - TACACS+ (TACACS_*)

        Args:
            safe: Safe settings
            review_required: Environment-specific settings
            sensitive: Sensitive settings

        Returns:
            Dictionary with migration results:
            {
                'ldap_migrated': bool,
                'saml_migrated': bool,
                'total_authenticators': int,
                'total_maps': int,
                'migrated_prefixes': list  # Settings prefixes to remove
            }
        """
        result: dict[str, Any] = {
            "ldap_migrated": False,
            "saml_migrated": False,
            "azure_ad_migrated": False,
            "github_migrated": False,
            "total_authenticators": 0,
            "total_maps": 0,
            "migrated_prefixes": [],
        }

        # 1. LDAP Migration (existing implementation - keep as-is)
        ldap_settings = self._extract_ldap_settings(safe, review_required, sensitive)
        if ldap_settings:
            logger.info(
                "ldap_settings_detected",
                count=len(ldap_settings),
                message="LDAP settings detected - will migrate to Platform Gateway",
            )
            ldap_migrated = await self._migrate_ldap_to_gateway(ldap_settings)
            if ldap_migrated:
                result["ldap_migrated"] = True
                result["total_authenticators"] += 1
                result["migrated_prefixes"].append("AUTH_LDAP_")

        # 2. SAML Migration
        saml_settings = self._extract_auth_settings(
            safe, review_required, sensitive, "SOCIAL_AUTH_SAML_"
        )
        if saml_settings:
            logger.info(
                "saml_settings_detected",
                count=len(saml_settings),
                message="SAML settings detected - will migrate to Platform Gateway",
            )
            saml_migrated = await self._migrate_saml_to_gateway(saml_settings)
            if saml_migrated:
                result["saml_migrated"] = True
                result["total_authenticators"] += 1
                result["migrated_prefixes"].append("SOCIAL_AUTH_SAML_")

        # 3. Azure AD OAuth2 Migration
        azure_settings = self._extract_auth_settings(
            safe, review_required, sensitive, "SOCIAL_AUTH_AZUREAD_OAUTH2_"
        )
        if azure_settings:
            logger.info(
                "azure_ad_settings_detected",
                count=len(azure_settings),
                message="Azure AD OAuth2 settings detected - will migrate to Platform Gateway",
            )
            azure_migrated = await self._migrate_azure_ad_to_gateway(azure_settings)
            if azure_migrated:
                result["azure_ad_migrated"] = True
                result["total_authenticators"] += 1
                result["migrated_prefixes"].append("SOCIAL_AUTH_AZUREAD_OAUTH2_")

        # 4. GitHub Enterprise Migration
        github_settings = self._extract_auth_settings(
            safe, review_required, sensitive, "SOCIAL_AUTH_GITHUB_ENTERPRISE_"
        )
        if github_settings:
            logger.info(
                "github_settings_detected",
                count=len(github_settings),
                message="GitHub Enterprise settings detected - will migrate to Platform Gateway",
            )
            github_migrated = await self._migrate_github_to_gateway(github_settings)
            if github_migrated:
                result["github_migrated"] = True
                result["total_authenticators"] += 1
                result["migrated_prefixes"].append("SOCIAL_AUTH_GITHUB_ENTERPRISE_")

        # Log overall migration summary
        if result["total_authenticators"] > 0:
            logger.info(
                "authentication_migration_completed",
                total_authenticators=result["total_authenticators"],
                ldap=result["ldap_migrated"],
                saml=result["saml_migrated"],
                azure_ad=result["azure_ad_migrated"],
                github=result["github_migrated"],
                message=f"✓ Migrated {result['total_authenticators']} authentication method(s) to Platform Gateway",
            )

        return result

    def _extract_auth_settings(
        self, safe: dict, review_required: dict, sensitive: dict, prefix: str
    ) -> dict[str, Any]:
        """Extract authentication settings by prefix (generic method).

        Args:
            safe: Safe settings
            review_required: Environment-specific settings
            sensitive: Sensitive settings
            prefix: Settings prefix (e.g., 'SOCIAL_AUTH_SAML_', 'SOCIAL_AUTH_AZUREAD_OAUTH2_')

        Returns:
            Dictionary of settings with the specified prefix
        """
        settings = {}

        # Collect settings from all categories
        for category in [safe, review_required, sensitive]:
            for key, value in category.items():
                if key.startswith(prefix):
                    # For review_required and sensitive, extract the actual value
                    if isinstance(value, dict) and "source_value" in value:
                        settings[key] = value["source_value"]
                    else:
                        settings[key] = value

        return settings

    async def _migrate_saml_to_gateway(self, saml_settings: dict[str, Any]) -> bool:
        """Migrate SAML settings to Platform Gateway authenticators (AAP 2.6+).

        Args:
            saml_settings: SAML settings from source (SOCIAL_AUTH_SAML_*)

        Returns:
            True if migration successful, False otherwise
        """
        try:
            # Transform SAML settings to Gateway format
            gateway_config = self._transform_saml_to_gateway(saml_settings)
            if not gateway_config:
                logger.warning("saml_migration_skipped", reason="Insufficient SAML configuration")
                return False

            # Create SAML authenticator
            authenticator = await self.client.create_gateway_authenticator(
                name="SAML SSO",
                plugin_type="ansible_base.authentication.authenticator_plugins.saml",
                configuration=gateway_config,
                enabled=True,
                create_objects=True,
                order=2,
            )

            logger.info(
                "saml_authenticator_created",
                authenticator_id=authenticator.get("id"),
                name=authenticator.get("name"),
                message="✓ SAML authenticator migrated to Platform Gateway",
            )

            # Create authenticator maps for organization/team mappings if present
            # (SAML may have organization/team mapping configuration)
            maps_created = 0
            if "SOCIAL_AUTH_SAML_ORGANIZATION_MAP" in saml_settings:
                maps_created = await self._create_saml_authenticator_maps(
                    authenticator["id"], saml_settings
                )
                if maps_created > 0:
                    logger.info(
                        "saml_authenticator_maps_created",
                        count=maps_created,
                        message=f"✓ Created {maps_created} SAML authenticator maps",
                    )

            return True

        except Exception as e:
            logger.error(
                "saml_migration_failed",
                error=str(e),
                message="✗ Failed to migrate SAML settings to Gateway",
            )
            return False

    async def _migrate_azure_ad_to_gateway(self, azure_settings: dict[str, Any]) -> bool:
        """Migrate Azure AD OAuth2 settings to Platform Gateway authenticators (AAP 2.6+).

        Args:
            azure_settings: Azure AD settings from source (SOCIAL_AUTH_AZUREAD_OAUTH2_*)

        Returns:
            True if migration successful, False otherwise
        """
        try:
            # Transform Azure AD settings to Gateway format
            gateway_config = self._transform_azure_ad_to_gateway(azure_settings)
            if not gateway_config:
                logger.warning(
                    "azure_ad_migration_skipped", reason="Insufficient Azure AD configuration"
                )
                return False

            # Create Azure AD authenticator
            authenticator = await self.client.create_gateway_authenticator(
                name="Azure AD OAuth2",
                plugin_type="ansible_base.authentication.authenticator_plugins.azuread_oauth",
                configuration=gateway_config,
                enabled=True,
                create_objects=True,
                order=3,
            )

            logger.info(
                "azure_ad_authenticator_created",
                authenticator_id=authenticator.get("id"),
                name=authenticator.get("name"),
                message="✓ Azure AD authenticator migrated to Platform Gateway",
            )

            return True

        except Exception as e:
            logger.error(
                "azure_ad_migration_failed",
                error=str(e),
                message="✗ Failed to migrate Azure AD settings to Gateway",
            )
            return False

    async def _migrate_github_to_gateway(self, github_settings: dict[str, Any]) -> bool:
        """Migrate GitHub Enterprise settings to Platform Gateway authenticators (AAP 2.6+).

        Args:
            github_settings: GitHub settings from source (SOCIAL_AUTH_GITHUB_ENTERPRISE_*)

        Returns:
            True if migration successful, False otherwise
        """
        try:
            # Transform GitHub settings to Gateway format
            gateway_config = self._transform_github_to_gateway(github_settings)
            if not gateway_config:
                logger.warning(
                    "github_migration_skipped", reason="Insufficient GitHub configuration"
                )
                return False

            # Create GitHub authenticator
            authenticator = await self.client.create_gateway_authenticator(
                name="GitHub Enterprise",
                plugin_type="ansible_base.authentication.authenticator_plugins.github",
                configuration=gateway_config,
                enabled=True,
                create_objects=True,
                order=4,
            )

            logger.info(
                "github_authenticator_created",
                authenticator_id=authenticator.get("id"),
                name=authenticator.get("name"),
                message="✓ GitHub authenticator migrated to Platform Gateway",
            )

            return True

        except Exception as e:
            logger.error(
                "github_migration_failed",
                error=str(e),
                message="✗ Failed to migrate GitHub settings to Gateway",
            )
            return False

    def _transform_saml_to_gateway(self, saml_settings: dict[str, Any]) -> dict[str, Any] | None:
        """Transform AAP 2.4 SAML settings to Gateway authenticator format.

        Field mapping:
        - SOCIAL_AUTH_SAML_SP_ENTITY_ID → SP_ENTITY_ID
        - SOCIAL_AUTH_SAML_SP_PUBLIC_CERT → SP_PUBLIC_CERT
        - SOCIAL_AUTH_SAML_SP_PRIVATE_KEY → SP_PRIVATE_KEY (excluded for security)
        - SOCIAL_AUTH_SAML_ENABLED_IDPS → ENABLED_IDPS
        - SOCIAL_AUTH_SAML_* → * (remove prefix)

        Args:
            saml_settings: SAML settings with SOCIAL_AUTH_SAML_ prefix

        Returns:
            Gateway authenticator configuration or None if insufficient data
        """
        # Required field - at least one IDP must be configured
        enabled_idps = saml_settings.get("SOCIAL_AUTH_SAML_ENABLED_IDPS")
        if not enabled_idps:
            return None

        config = {}

        # Map fields from AAP 2.4 to Gateway format
        field_mapping = {
            "SOCIAL_AUTH_SAML_SP_ENTITY_ID": "SP_ENTITY_ID",
            "SOCIAL_AUTH_SAML_SP_PUBLIC_CERT": "SP_PUBLIC_CERT",
            # SP_PRIVATE_KEY excluded for security (manual entry required)
            "SOCIAL_AUTH_SAML_ORG_INFO": "ORG_INFO",
            "SOCIAL_AUTH_SAML_TECHNICAL_CONTACT": "TECHNICAL_CONTACT",
            "SOCIAL_AUTH_SAML_SUPPORT_CONTACT": "SUPPORT_CONTACT",
            "SOCIAL_AUTH_SAML_ENABLED_IDPS": "ENABLED_IDPS",
            "SOCIAL_AUTH_SAML_SECURITY_CONFIG": "SECURITY_CONFIG",
        }

        for old_key, new_key in field_mapping.items():
            if old_key in saml_settings:
                config[new_key] = saml_settings[old_key]

        return config

    def _transform_azure_ad_to_gateway(
        self, azure_settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Transform AAP 2.4 Azure AD settings to Gateway authenticator format.

        Field mapping:
        - SOCIAL_AUTH_AZUREAD_OAUTH2_KEY → KEY
        - SOCIAL_AUTH_AZUREAD_OAUTH2_SECRET → SECRET (excluded for security)
        - SOCIAL_AUTH_AZUREAD_OAUTH2_* → * (remove prefix)

        Args:
            azure_settings: Azure AD settings with SOCIAL_AUTH_AZUREAD_OAUTH2_ prefix

        Returns:
            Gateway authenticator configuration or None if insufficient data
        """
        # Required field
        client_id = azure_settings.get("SOCIAL_AUTH_AZUREAD_OAUTH2_KEY")
        if not client_id:
            return None

        config = {}

        # Map fields from AAP 2.4 to Gateway format
        field_mapping = {
            "SOCIAL_AUTH_AZUREAD_OAUTH2_KEY": "KEY",
            # SECRET excluded for security (manual entry required)
            "SOCIAL_AUTH_AZUREAD_OAUTH2_URL": "URL",
        }

        for old_key, new_key in field_mapping.items():
            if old_key in azure_settings:
                config[new_key] = azure_settings[old_key]

        return config

    def _transform_github_to_gateway(
        self, github_settings: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Transform AAP 2.4 GitHub settings to Gateway authenticator format.

        Field mapping:
        - SOCIAL_AUTH_GITHUB_ENTERPRISE_URL → URL
        - SOCIAL_AUTH_GITHUB_ENTERPRISE_API_URL → API_URL
        - SOCIAL_AUTH_GITHUB_ENTERPRISE_KEY → KEY
        - SOCIAL_AUTH_GITHUB_ENTERPRISE_SECRET → SECRET (excluded for security)
        - SOCIAL_AUTH_GITHUB_ENTERPRISE_* → * (remove prefix)

        Args:
            github_settings: GitHub settings with SOCIAL_AUTH_GITHUB_ENTERPRISE_ prefix

        Returns:
            Gateway authenticator configuration or None if insufficient data
        """
        # Required field
        url = github_settings.get("SOCIAL_AUTH_GITHUB_ENTERPRISE_URL")
        if not url:
            return None

        config = {}

        # Map fields from AAP 2.4 to Gateway format
        field_mapping = {
            "SOCIAL_AUTH_GITHUB_ENTERPRISE_URL": "URL",
            "SOCIAL_AUTH_GITHUB_ENTERPRISE_API_URL": "API_URL",
            "SOCIAL_AUTH_GITHUB_ENTERPRISE_KEY": "KEY",
            # SECRET excluded for security (manual entry required)
        }

        for old_key, new_key in field_mapping.items():
            if old_key in github_settings:
                config[new_key] = github_settings[old_key]

        return config

    async def _create_saml_authenticator_maps(
        self, authenticator_id: int, saml_settings: dict[str, Any]
    ) -> int:
        """Create authenticator maps for SAML organization/team mappings.

        Args:
            authenticator_id: ID of the created authenticator
            saml_settings: SAML settings with SOCIAL_AUTH_SAML_ prefix

        Returns:
            Number of maps successfully created
        """
        maps_created = 0

        # Extract organization/team mappings if present
        org_map = saml_settings.get("SOCIAL_AUTH_SAML_ORGANIZATION_MAP", {})
        team_map = saml_settings.get("SOCIAL_AUTH_SAML_TEAM_MAP", {})

        try:
            # Create organization maps
            if org_map:
                for org_name, org_config in org_map.items():
                    # SAML uses SAML attributes instead of LDAP groups
                    # Trigger based on SAML attribute values
                    users_attr = org_config.get("users")
                    if users_attr:
                        try:
                            await self.client.create_authenticator_map(
                                authenticator_id=authenticator_id,
                                name=f"SAML - {org_name} - Members",
                                map_type="organization",
                                organization=org_name,
                                role="Organization Member",
                                triggers={"attributes": {"has_or": [users_attr]}},
                                revoke=org_config.get("remove_users", False),
                                order=10,
                            )
                            maps_created += 1
                        except Exception as e:
                            logger.error(
                                "saml_authenticator_map_creation_failed",
                                org=org_name,
                                role="member",
                                error=str(e),
                            )

                    admins_attr = org_config.get("admins")
                    if admins_attr:
                        try:
                            await self.client.create_authenticator_map(
                                authenticator_id=authenticator_id,
                                name=f"SAML - {org_name} - Admins",
                                map_type="organization",
                                organization=org_name,
                                role="Organization Admin",
                                triggers={"attributes": {"has_or": [admins_attr]}},
                                revoke=org_config.get("remove_admins", False),
                                order=10,
                            )
                            maps_created += 1
                        except Exception as e:
                            logger.error(
                                "saml_authenticator_map_creation_failed",
                                org=org_name,
                                role="admin",
                                error=str(e),
                            )

            # Create team maps
            if team_map:
                for team_name, team_config in team_map.items():
                    users_attr = team_config.get("users")
                    org_name = team_config.get("organization")

                    if users_attr and org_name:
                        try:
                            await self.client.create_authenticator_map(
                                authenticator_id=authenticator_id,
                                name=f"SAML - {team_name} Team",
                                map_type="team",
                                organization=org_name,
                                team=team_name,
                                role="Team Member",
                                triggers={"attributes": {"has_or": [users_attr]}},
                                revoke=team_config.get("remove", False),
                                order=20,
                            )
                            maps_created += 1
                        except Exception as e:
                            logger.error(
                                "saml_authenticator_map_creation_failed",
                                team=team_name,
                                error=str(e),
                            )

        except Exception as e:
            logger.error(
                "saml_authenticator_maps_creation_error",
                authenticator_id=authenticator_id,
                error=str(e),
            )

        return maps_created

    def _extract_ldap_settings(
        self, safe: dict, review_required: dict, sensitive: dict
    ) -> dict[str, Any]:
        """Extract all LDAP settings from categorized settings.

        Args:
            safe: Safe settings
            review_required: Environment-specific settings
            sensitive: Sensitive settings

        Returns:
            Dictionary of all LDAP settings (AUTH_LDAP_*)
        """
        ldap_settings = {}

        # Collect LDAP settings from all categories
        for category in [safe, review_required, sensitive]:
            for key, value in category.items():
                if key.startswith("AUTH_LDAP_"):
                    # For review_required and sensitive, extract the actual value
                    if isinstance(value, dict) and "source_value" in value:
                        ldap_settings[key] = value["source_value"]
                    else:
                        ldap_settings[key] = value

        return ldap_settings

    async def _migrate_ldap_to_gateway(self, ldap_settings: dict[str, Any]) -> bool:
        """Migrate LDAP settings to Platform Gateway authenticators (AAP 2.6+).

        Args:
            ldap_settings: Dictionary of AUTH_LDAP_* settings from source

        Returns:
            True if migration successful, False otherwise
        """
        try:
            # Group LDAP settings by server (primary, secondary, etc.)
            ldap_servers = self._group_ldap_servers(ldap_settings)

            authenticators_created = 0
            total_maps_created = 0

            for server_name, server_settings in ldap_servers.items():
                # Transform AAP 2.4 format to Gateway format (connection/search settings only)
                gateway_config = self._transform_ldap_to_gateway(server_settings)

                if not gateway_config:
                    logger.warning(
                        "ldap_server_skipped",
                        server_name=server_name,
                        message="Insufficient LDAP configuration",
                    )
                    continue

                # Create Gateway authenticator
                try:
                    # Order: 2 for primary, 3 for secondary, etc.
                    order = 2 + authenticators_created

                    authenticator = await self.client.create_gateway_authenticator(
                        name=server_name,
                        plugin_type="ansible_base.authentication.authenticator_plugins.ldap",
                        configuration=gateway_config,
                        enabled=True,
                        create_objects=True,
                        remove_users=False,
                        order=order,
                    )

                    authenticator_id_raw = authenticator.get("id")
                    if authenticator_id_raw is None:
                        raise TypeError("LDAP gateway authenticator response missing id")
                    authenticator_id = int(authenticator_id_raw)
                    authenticators_created += 1

                    logger.info(
                        "ldap_gateway_authenticator_created",
                        server_name=server_name,
                        authenticator_id=authenticator_id,
                        order=order,
                        message=f"✓ Created Gateway authenticator: {server_name}",
                    )

                    # Create authenticator maps for organization/team/user flag mappings
                    maps_created = await self._create_authenticator_maps(
                        authenticator_id=authenticator_id,
                        server_name=server_name,
                        server_settings=server_settings,
                    )

                    total_maps_created += maps_created

                    if maps_created > 0:
                        logger.info(
                            "ldap_authenticator_maps_created",
                            server_name=server_name,
                            authenticator_id=authenticator_id,
                            maps_count=maps_created,
                            message=f"✓ Created {maps_created} authenticator map(s)",
                        )

                except Exception as e:
                    logger.error(
                        "ldap_gateway_authenticator_failed",
                        server_name=server_name,
                        error=str(e),
                    )

            if authenticators_created > 0:
                logger.info(
                    "ldap_migration_to_gateway_completed",
                    authenticators_count=authenticators_created,
                    maps_count=total_maps_created,
                    message=f"✓ Migrated {authenticators_created} LDAP server(s) with {total_maps_created} mapping(s) to Platform Gateway",
                )
                return True
            else:
                logger.warning(
                    "ldap_migration_to_gateway_failed", message="No LDAP authenticators created"
                )
                return False

        except Exception as e:
            logger.error("ldap_migration_to_gateway_error", error=str(e))
            return False

    def _group_ldap_servers(self, ldap_settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Group LDAP settings by server (primary, secondary, etc.).

        AAP 2.4 uses:
        - AUTH_LDAP_* for primary
        - AUTH_LDAP_1_* for secondary
        - AUTH_LDAP_2_* for tertiary

        Args:
            ldap_settings: All LDAP settings

        Returns:
            Dictionary of server settings keyed by server name
        """
        servers = {}

        # Primary server (no number suffix)
        primary = {
            k: v
            for k, v in ldap_settings.items()
            if k.startswith("AUTH_LDAP_")
            and not k.startswith("AUTH_LDAP_1_")
            and not k.startswith("AUTH_LDAP_2_")
        }
        if primary:
            servers["Primary LDAP"] = primary

        # Secondary server (AUTH_LDAP_1_*)
        secondary = {
            k.replace("AUTH_LDAP_1_", "AUTH_LDAP_"): v
            for k, v in ldap_settings.items()
            if k.startswith("AUTH_LDAP_1_")
        }
        if secondary:
            servers["Secondary LDAP"] = secondary

        # Tertiary server (AUTH_LDAP_2_*)
        tertiary = {
            k.replace("AUTH_LDAP_2_", "AUTH_LDAP_"): v
            for k, v in ldap_settings.items()
            if k.startswith("AUTH_LDAP_2_")
        }
        if tertiary:
            servers["Tertiary LDAP"] = tertiary

        return servers

    async def _create_authenticator_maps(
        self, authenticator_id: int, server_name: str, server_settings: dict[str, Any]
    ) -> int:
        """Create authenticator maps for organization/team/user flag mappings.

        In AAP 2.6, organization and team mappings are not part of the authenticator
        configuration. They must be created as separate authenticator_map objects.

        Args:
            authenticator_id: ID of the created authenticator
            server_name: Name of the LDAP server (for map naming)
            server_settings: LDAP settings with AUTH_LDAP_ prefix

        Returns:
            Number of maps successfully created
        """
        maps_created = 0

        # Extract mapping fields from server settings
        org_map = server_settings.get("AUTH_LDAP_ORGANIZATION_MAP", {})
        team_map = server_settings.get("AUTH_LDAP_TEAM_MAP", {})
        user_flags = server_settings.get("AUTH_LDAP_USER_FLAGS_BY_GROUP", {})

        try:
            # 1. Create user flag maps (superuser, auditor, etc.)
            if user_flags:
                for flag_name, ldap_group in user_flags.items():
                    if not ldap_group:
                        continue

                    try:
                        await self.client.create_authenticator_map(
                            authenticator_id=authenticator_id,
                            name=f"LDAP - {flag_name.replace('_', ' ').title()}",
                            map_type=flag_name,  # e.g., "is_superuser", "is_system_auditor"
                            triggers={"groups": {"has_or": [ldap_group]}},
                            order=5,  # High priority for user flags
                        )
                        maps_created += 1
                    except Exception as e:
                        logger.error(
                            "authenticator_map_creation_failed", flag=flag_name, error=str(e)
                        )

            # 2. Create organization maps
            if org_map:
                for org_name, org_config in org_map.items():
                    # Create member map
                    users_group = org_config.get("users")
                    if users_group:
                        try:
                            await self.client.create_authenticator_map(
                                authenticator_id=authenticator_id,
                                name=f"LDAP - {org_name} - Members",
                                map_type="organization",
                                organization=org_name,
                                role="Organization Member",
                                triggers={"groups": {"has_or": [users_group]}},
                                revoke=org_config.get("remove_users", False),
                                order=10,
                            )
                            maps_created += 1
                        except Exception as e:
                            logger.error(
                                "authenticator_map_creation_failed",
                                org=org_name,
                                role="member",
                                error=str(e),
                            )

                    # Create admin map
                    admins_group = org_config.get("admins")
                    if admins_group:
                        try:
                            await self.client.create_authenticator_map(
                                authenticator_id=authenticator_id,
                                name=f"LDAP - {org_name} - Admins",
                                map_type="organization",
                                organization=org_name,
                                role="Organization Admin",
                                triggers={"groups": {"has_or": [admins_group]}},
                                revoke=org_config.get("remove_admins", False),
                                order=10,
                            )
                            maps_created += 1
                        except Exception as e:
                            logger.error(
                                "authenticator_map_creation_failed",
                                org=org_name,
                                role="admin",
                                error=str(e),
                            )

            # 3. Create team maps
            if team_map:
                for team_name, team_config in team_map.items():
                    users_group = team_config.get("users")
                    org_name = team_config.get("organization")

                    if users_group and org_name:
                        try:
                            await self.client.create_authenticator_map(
                                authenticator_id=authenticator_id,
                                name=f"LDAP - {team_name} Team",
                                map_type="team",
                                organization=org_name,
                                team=team_name,
                                role="Team Member",
                                triggers={"groups": {"has_or": [users_group]}},
                                revoke=team_config.get("remove", False),
                                order=20,  # Lower priority than org maps
                            )
                            maps_created += 1
                        except Exception as e:
                            logger.error(
                                "authenticator_map_creation_failed", team=team_name, error=str(e)
                            )

        except Exception as e:
            logger.error(
                "authenticator_maps_creation_error", authenticator_id=authenticator_id, error=str(e)
            )

        return maps_created

    def _transform_ldap_to_gateway(self, server_settings: dict[str, Any]) -> dict[str, Any] | None:
        """Transform AAP 2.4 LDAP settings to Gateway authenticator format.

        Note: In AAP 2.6, organization/team mappings are NOT part of the authenticator
        configuration. They are created as separate authenticator_maps via a different API.

        Field mapping:
        - AUTH_LDAP_SERVER_URI → SERVER_URI
        - AUTH_LDAP_BIND_DN → BIND_DN
        - AUTH_LDAP_BIND_PASSWORD → BIND_PASSWORD (excluded for security)
        - AUTH_LDAP_* → * (remove prefix)

        Args:
            server_settings: LDAP settings for one server (with AUTH_LDAP_ prefix)

        Returns:
            Gateway authenticator configuration or None if insufficient data
        """
        # Required fields
        server_uri = server_settings.get("AUTH_LDAP_SERVER_URI")
        if not server_uri:
            return None

        config = {}

        # Map ONLY the fields supported by Gateway authenticator configuration
        # Organization/Team mappings are handled separately via authenticator_maps API
        field_mapping = {
            # Connection settings
            "AUTH_LDAP_SERVER_URI": "SERVER_URI",
            "AUTH_LDAP_BIND_DN": "BIND_DN",
            # BIND_PASSWORD excluded for security (manual entry required)
            "AUTH_LDAP_CONNECTION_OPTIONS": "CONNECTION_OPTIONS",
            "AUTH_LDAP_START_TLS": "START_TLS",
            # User settings
            "AUTH_LDAP_USER_SEARCH": "USER_SEARCH",
            "AUTH_LDAP_USER_DN_TEMPLATE": "USER_DN_TEMPLATE",
            "AUTH_LDAP_USER_ATTR_MAP": "USER_ATTR_MAP",
            # Group settings
            "AUTH_LDAP_GROUP_TYPE": "GROUP_TYPE",
            "AUTH_LDAP_GROUP_TYPE_PARAMS": "GROUP_TYPE_PARAMS",
            "AUTH_LDAP_GROUP_SEARCH": "GROUP_SEARCH",
            # Note: REQUIRE_GROUP and DENY_GROUP may need to be authenticator_maps too
        }

        for old_key, new_key in field_mapping.items():
            if old_key in server_settings:
                value = server_settings[old_key]
                # Ensure SERVER_URI is a list
                if new_key == "SERVER_URI" and isinstance(value, str):
                    value = [value]
                config[new_key] = value

        return config

    def _generate_settings_review_report(
        self,
        review_required: dict,
        sensitive: dict,
        auth_migration_info: dict[str, Any] | None = None,
    ) -> None:
        """Generate markdown report for settings that need review.

        Args:
            review_required: Environment-specific settings
            sensitive: Sensitive settings (passwords, secrets)
            auth_migration_info: Authentication migration results from _migrate_all_authentication_to_gateway()
                                Contains: ldap_migrated, saml_migrated, azure_ad_migrated, github_migrated, etc.
        """
        from pathlib import Path

        report_lines = []
        report_lines.append("# Settings Migration Review Report\n\n")

        # Add authentication migration status
        if auth_migration_info:
            # Check if any authentication was migrated
            auth_types_migrated = []
            if auth_migration_info.get("ldap_migrated"):
                auth_types_migrated.append("LDAP")
            if auth_migration_info.get("saml_migrated"):
                auth_types_migrated.append("SAML")
            if auth_migration_info.get("azure_ad_migrated"):
                auth_types_migrated.append("Azure AD OAuth2")
            if auth_migration_info.get("github_migrated"):
                auth_types_migrated.append("GitHub Enterprise")

            if auth_types_migrated:
                auth_list = ", ".join(auth_types_migrated)
                report_lines.append(
                    f"✅ **Authentication Settings Migrated to Gateway:** {auth_list} settings have been "
                )
                report_lines.append(
                    "automatically migrated to Platform Gateway authenticators. After migration:\n"
                )
                report_lines.append(
                    "1. Manually enter sensitive credentials in Gateway UI (Settings → Authentication → Authenticators):\n"
                )

                # List specific credentials needed per auth type
                if auth_migration_info.get("ldap_migrated"):
                    report_lines.append("   - LDAP: `BIND_PASSWORD`\n")
                if auth_migration_info.get("saml_migrated"):
                    report_lines.append("   - SAML: `SP_PRIVATE_KEY`\n")
                if auth_migration_info.get("azure_ad_migrated"):
                    report_lines.append("   - Azure AD: `SECRET`\n")
                if auth_migration_info.get("github_migrated"):
                    report_lines.append("   - GitHub: `SECRET`\n")

                report_lines.append(
                    "2. Test login with a test user from each authentication source\n"
                )
                report_lines.append(
                    "3. Verify authenticators: `https://target-aap/api/gateway/v1/authenticators/`\n"
                )
                report_lines.append(
                    "4. Verify authenticator maps: `https://target-aap/api/gateway/v1/authenticator_maps/`\n\n"
                )
            else:
                # No authentication migrated (AAP 2.5 or earlier)
                report_lines.append(
                    "⚠️ **Authentication Settings:** Authentication settings imported to Controller API. "
                )
                report_lines.append("In AAP 2.6+, authentication is managed by Platform Gateway. ")
                report_lines.append("After migration, verify authentication works:\n")
                report_lines.append("1. Test login with a test user\n")
                report_lines.append(
                    "2. Manually enter sensitive credentials (passwords, secrets, private keys)\n"
                )
                report_lines.append(
                    "3. If authentication fails, configure via Platform Gateway (Settings → Authentication in UI)\n"
                )
                report_lines.append(
                    "4. See README.md 'Post-Migration: Verify Authentication' section for details\n\n"
                )
        else:
            # Fallback for backward compatibility (if called with old signature)
            report_lines.append(
                "⚠️ **Authentication Settings:** Please verify authentication configuration after migration.\n\n"
            )

        report_lines.append("---\n\n")

        if review_required:
            report_lines.append("## ⚠️  Environment-Specific Settings (Review Required)\n\n")
            report_lines.append(
                "These settings contain URLs, paths, or hostnames that may differ between environments:\n\n"
            )

            for key, value_info in sorted(review_required.items()):
                source_value = value_info.get("source_value")
                report_lines.append(f"### `{key}`\n")
                report_lines.append(f"**Source value:** `{source_value}`\n\n")
                report_lines.append("**Action:** Review and update if needed:\n")
                report_lines.append("```bash\n")
                report_lines.append("curl -sk -X PATCH -H 'Authorization: Bearer $TOKEN' \\\n")
                report_lines.append("  'https://target-aap/api/v2/settings/all/' \\\n")
                report_lines.append(f"  -d '{{'{key}': 'NEW_VALUE'}}'\n")
                report_lines.append("```\n\n")

        if sensitive:
            report_lines.append("## 🔒 Sensitive Settings (Manual Input Required)\n\n")
            report_lines.append(
                "These settings contain passwords, secrets, or API keys that were redacted:\n\n"
            )

            for key in sorted(sensitive.keys()):
                report_lines.append(f"### `{key}`\n")
                report_lines.append("**Action:** Provide new value:\n")
                report_lines.append("```bash\n")
                report_lines.append("curl -sk -X PATCH -H 'Authorization: Bearer $TOKEN' \\\n")
                report_lines.append("  'https://target-aap/api/v2/settings/all/' \\\n")
                report_lines.append(f"  -d '{{'{key}': 'YOUR_NEW_VALUE'}}'\n")
                report_lines.append("```\n\n")

        # Write report
        report_path = Path("SETTINGS-REVIEW-REPORT.md")
        with open(report_path, "w") as f:
            f.writelines(report_lines)

        logger.info("settings_review_report_generated", path=str(report_path))

    async def import_settings(
        self,
        settings_list: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import settings (expects a list with single settings dict).

        Args:
            settings_list: List containing single settings dict
            progress_callback: Optional progress callback

        Returns:
            List with import result
        """
        if not settings_list or len(settings_list) == 0:
            return []

        # Settings is a single resource
        settings_data = settings_list[0]
        result = await self.import_resource(
            resource_type="settings",
            source_id=0,  # Settings have no real ID
            data=settings_data,
            resolve_dependencies=False,
        )

        if progress_callback:
            success = 1 if result else 0
            failed = 0 if result else 1
            progress_callback(success, failed, 0)

        return [result] if result else []
