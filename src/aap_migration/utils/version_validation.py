"""Version validation utilities for AAP migrations.

This module provides functions to validate AAP version compatibility
between source and target instances for safe migrations.
"""

from typing import Tuple

from aap_migration.utils.logging import get_logger

logger = get_logger(__name__)


class VersionValidationError(Exception):
    """Raised when version compatibility check fails."""

    pass


def parse_version(version_string: str) -> Tuple[int, int, int]:
    """Parse a version string into major, minor, patch components.

    Args:
        version_string: Version string (e.g., "2.4.0", "2.6.1")

    Returns:
        Tuple of (major, minor, patch) as integers

    Raises:
        ValueError: If version string is invalid
    """
    try:
        # Handle versions with extra components (e.g., "2.4.0-dev" -> "2.4.0")
        version_core = version_string.split("-")[0].split("+")[0]
        parts = version_core.split(".")

        # Default to 0 for missing components
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0

        return (major, minor, patch)
    except (ValueError, IndexError) as e:
        raise ValueError(f"Invalid version string: {version_string}") from e


def validate_version_compatibility(
    source_version: str,
    target_version: str,
    min_source_version: str = "2.3.0",
    min_target_version: str = "2.5.0",
) -> None:
    """Validate that source and target AAP versions are compatible for migration.

    AAP Bridge supports:
    - Source: AAP 2.3+, 2.4+, 2.5+
    - Target: AAP 2.5+, 2.6+

    Migration requirements:
    - Source version must be >= 2.3.0
    - Target version must be >= 2.5.0
    - Target version should be >= source version (no downgrade migrations)

    Args:
        source_version: Source AAP version string (e.g., "2.4.0")
        target_version: Target AAP version string (e.g., "2.6.0")
        min_source_version: Minimum supported source version (default: "2.3.0")
        min_target_version: Minimum supported target version (default: "2.5.0")

    Raises:
        VersionValidationError: If versions are incompatible

    Example:
        >>> validate_version_compatibility("2.4.1", "2.6.0")  # OK
        >>> validate_version_compatibility("2.2.0", "2.6.0")  # Raises error
        >>> validate_version_compatibility("2.6.0", "2.4.0")  # Raises error (downgrade)
    """
    try:
        source_parsed = parse_version(source_version)
        target_parsed = parse_version(target_version)
        min_source_parsed = parse_version(min_source_version)
        min_target_parsed = parse_version(min_target_version)
    except ValueError as e:
        raise VersionValidationError(f"Version parsing failed: {e}") from e

    # Check minimum source version
    if source_parsed < min_source_parsed:
        raise VersionValidationError(
            f"Source AAP version {source_version} is below minimum supported version "
            f"{min_source_version}. AAP Bridge requires source AAP >= {min_source_version}."
        )

    # Check minimum target version
    if target_parsed < min_target_parsed:
        raise VersionValidationError(
            f"Target AAP version {target_version} is below minimum supported version "
            f"{min_target_version}. AAP Bridge requires target AAP >= {min_target_version}."
        )

    # Check for downgrade migration (target < source)
    if target_parsed < source_parsed:
        logger.warning(
            "downgrade_migration_detected",
            source_version=source_version,
            target_version=target_version,
            message=(
                "Target version is older than source version. Downgrade migrations "
                "may have compatibility issues and are not recommended."
            ),
        )
        # Note: We log a warning but don't raise an error, as some downgrade
        # migrations may work if the user knows what they're doing

    # Log successful validation
    logger.info(
        "version_compatibility_validated",
        source_version=source_version,
        target_version=target_version,
        source_parsed=f"{source_parsed[0]}.{source_parsed[1]}.{source_parsed[2]}",
        target_parsed=f"{target_parsed[0]}.{target_parsed[1]}.{target_parsed[2]}",
    )


def get_version_info(version_string: str) -> dict[str, int | str]:
    """Get detailed version information from a version string.

    Args:
        version_string: Version string (e.g., "2.4.0", "2.6.1")

    Returns:
        Dictionary with version components and metadata

    Example:
        >>> get_version_info("2.4.1")
        {'major': 2, 'minor': 4, 'patch': 1, 'version': '2.4.1', 'is_2_4': True}
    """
    try:
        major, minor, patch = parse_version(version_string)
        return {
            "major": major,
            "minor": minor,
            "patch": patch,
            "version": version_string,
            "is_2_3": major == 2 and minor == 3,
            "is_2_4": major == 2 and minor == 4,
            "is_2_5": major == 2 and minor == 5,
            "is_2_6": major == 2 and minor == 6,
            "supports_platform_gateway": major == 2 and minor >= 6,
            "supports_bulk_operations": major == 2 and minor >= 4,
        }
    except ValueError:
        logger.warning(
            "version_info_parse_failed",
            version_string=version_string,
            message="Failed to parse version string, returning empty info"
        )
        return {
            "major": 0,
            "minor": 0,
            "patch": 0,
            "version": version_string,
            "is_2_3": False,
            "is_2_4": False,
            "is_2_5": False,
            "is_2_6": False,
            "supports_platform_gateway": False,
            "supports_bulk_operations": False,
        }
