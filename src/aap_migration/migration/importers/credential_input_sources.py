from collections.abc import Callable
from typing import Any

from aap_migration.migration.importers.base import ResourceImporter
from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class CredentialInputSourceImporter(ResourceImporter):
    """Importer for credential input source resources.

    Credential input sources link credential input fields to values from other
    credentials (e.g. HashiCorp Vault). AAP exposes them with
    ``target_credential`` / ``source_credential`` fields.
    """

    DEPENDENCIES: dict[str, str] = {
        "target_credential": "credentials",
        "source_credential": "credentials",
    }

    async def import_resource(
        self,
        resource_type: str,
        source_id: int,
        data: dict[str, Any],
        resolve_dependencies: bool = True,
    ) -> dict[str, Any] | None:
        """Import one credential input source (planner / single-resource path)."""
        _ = resolve_dependencies
        results = await self.import_credential_input_sources(
            [{**data, "_source_id": source_id, "id": source_id}]
        )
        if not results:
            # Prefer last recorded import error for this source_id when available
            for err in reversed(self.import_errors):
                if err.get("source_id") == source_id:
                    return None
            self.stats["skipped_count"] += 1
            return {
                "_skipped": True,
                "_skip_reason": (
                    "Credential input source skipped — missing fields or "
                    "target/source credential not mapped"
                ),
            }
        return results[0]

    async def import_credential_input_sources(
        self,
        input_sources: list[dict[str, Any]],
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Import credential input sources via the AAP credential_input_sources API.

        Args:
            input_sources: List of credential input source data
            progress_callback: Optional callback for progress updates.

        Returns:
            List of created/mapped input source records
        """
        results: list[dict[str, Any]] = []

        for input_source in input_sources:
            source_id = input_source.pop("_source_id", input_source.get("id"))
            # Prefer AAP field name; accept legacy "credential" alias from older exports
            source_target_credential_id = input_source.get("target_credential")
            if source_target_credential_id is None:
                source_target_credential_id = input_source.get("credential")
            source_input_field_name = input_source.get("input_field_name")
            source_source_credential_id = input_source.get("source_credential")
            metadata = input_source.get("metadata") or {}
            description = input_source.get("description")

            if (
                source_target_credential_id is None
                or not source_input_field_name
                or source_source_credential_id is None
            ):
                logger.warning(
                    "credential_input_source_missing_fields",
                    source_id=source_id,
                    message="Skipping credential input source due to missing required fields",
                )
                self.stats["error_count"] += 1
                self.import_errors.append(
                    {
                        "source_id": int(source_id) if source_id is not None else None,
                        "error": "Missing target_credential, source_credential, or input_field_name",
                        "error_type": "ValidationError",
                    }
                )
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            source_id_int = int(source_id)

            if self.state.is_migrated("credential_input_sources", source_id_int):
                target_id = self.state.get_mapped_id("credential_input_sources", source_id_int)
                self.stats["skipped_count"] += 1
                if target_id is not None:
                    results.append(
                        {
                            "id": target_id,
                            "_already_migrated": True,
                            "_skip_reason": (f"Already migrated (target id {target_id})"),
                        }
                    )
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            target_credential_id = self.state.get_mapped_id(
                "credentials", int(source_target_credential_id)
            )
            if not target_credential_id:
                logger.warning(
                    "credential_input_source_target_credential_not_imported",
                    source_id=source_id,
                    target_credential_id=source_target_credential_id,
                    message="Skipping credential input source - target credential not found",
                )
                self.stats["error_count"] += 1
                self.import_errors.append(
                    {
                        "source_id": source_id_int,
                        "error": (
                            f"Target credential (source id {source_target_credential_id}) "
                            "was not migrated"
                        ),
                        "error_type": "DependencyError",
                    }
                )
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            target_source_credential_id = self.state.get_mapped_id(
                "credentials", int(source_source_credential_id)
            )
            if not target_source_credential_id:
                logger.warning(
                    "credential_input_source_source_credential_not_imported",
                    source_id=source_id,
                    source_credential_id=source_source_credential_id,
                    message="Skipping credential input source - source credential not found",
                )
                self.stats["error_count"] += 1
                self.import_errors.append(
                    {
                        "source_id": source_id_int,
                        "error": (
                            f"Source credential (source id {source_source_credential_id}) "
                            "was not migrated"
                        ),
                        "error_type": "DependencyError",
                    }
                )
                if progress_callback:
                    progress_callback(
                        self.stats["imported_count"],
                        self.stats["error_count"],
                        self.stats["skipped_count"],
                    )
                continue

            payload: dict[str, Any] = {
                "target_credential": target_credential_id,
                "source_credential": target_source_credential_id,
                "input_field_name": source_input_field_name,
            }
            if metadata:
                payload["metadata"] = metadata
            if description:
                payload["description"] = description

            self.state.mark_in_progress(
                resource_type="credential_input_sources",
                source_id=source_id_int,
                source_name=str(
                    input_source.get("name") or f"{source_input_field_name}@{target_credential_id}"
                ),
                phase="import",
            )

            try:
                # Prefer create via the credential_input_sources endpoint
                result = await self.client.post("credential_input_sources/", json_data=payload)
                if not isinstance(result, dict):
                    result = {"id": target_credential_id}

                created_id = int(result.get("id") or target_credential_id)
                self.state.mark_completed(
                    resource_type="credential_input_sources",
                    source_id=source_id_int,
                    target_id=created_id,
                    source_name=str(
                        input_source.get("name")
                        or f"{source_input_field_name}@{target_credential_id}"
                    ),
                    target_name=result.get("input_field_name") or source_input_field_name,
                )
                self.stats["imported_count"] += 1
                results.append(result)
                logger.info(
                    "credential_input_source_created",
                    source_id=source_id,
                    target_credential_id=target_credential_id,
                    source_credential_id=target_source_credential_id,
                    input_field=source_input_field_name,
                    target_id=created_id,
                )

            except Exception as e:
                self.stats["error_count"] += 1
                logger.error(
                    "credential_input_source_create_failed",
                    source_id=source_id,
                    target_credential_id=target_credential_id,
                    error=str(e),
                    exc_info=True,
                )
                self.import_errors.append(
                    {
                        "source_id": source_id_int,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }
                )
                self.state.mark_failed(
                    resource_type="credential_input_sources",
                    source_id=source_id_int,
                    error_message=str(e),
                )

            if progress_callback:
                progress_callback(
                    self.stats["imported_count"],
                    self.stats["error_count"],
                    self.stats["skipped_count"],
                )

        return results


# Factory function for creating importers
