"""Settings importer (split from importers.catalog; re-exported)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from aap_migration.migration.importers._settings_gateway import SettingsGatewayMixin
from aap_migration.migration.importers.base import (
    ResourceImporter,
    logger,
)


class SettingsImporter(SettingsGatewayMixin, ResourceImporter):
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
        from packaging import version

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
