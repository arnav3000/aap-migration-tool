"""Settings importer (split from importers.catalog; re-exported)."""

from typing import Any, cast

from aap_migration.migration.importers.base import (
    logger,
)


class SettingsGatewayMixin:
    """Gateway auth transforms split from settings.py (mixin)."""

    client: Any
    state: Any
    stats: dict[str, int]
    import_errors: list[dict[str, Any]]

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

                    authenticator_id = authenticator.get("id")
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
                        authenticator_id=cast(int, authenticator_id),
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
