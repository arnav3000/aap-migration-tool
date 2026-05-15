"""Resource quality and governance analysis for AAP instances."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DuplicateResource:
    """Represents a duplicate resource found within an organization."""

    name: str
    resource_type: str  # "job_template", "workflow_job_template", etc.
    count: int
    ids: list[int]
    severity: str  # "error", "warning", "info"
    impact: str  # Human-readable impact description
    recommendation: str  # Suggested fix
    details: list[dict[str, Any]] = field(default_factory=list)  # Full resource data

    @property
    def severity_emoji(self) -> str:
        """Get emoji for severity level."""
        return {
            "error": "🔴",
            "warning": "🟡",
            "info": "🔵",
        }.get(self.severity, "⚪")

    @property
    def resource_type_display(self) -> str:
        """Get display name for resource type."""
        type_map = {
            "job_templates": "Job Template",
            "workflow_job_templates": "Workflow",
            "inventories": "Inventory",
            "projects": "Project",
            "credentials": "Credential",
            "execution_environments": "Execution Environment",
            "credential_types": "Credential Type",
        }
        return type_map.get(self.resource_type, self.resource_type.replace("_", " ").title())


@dataclass
class NamingPattern:
    """Naming pattern statistics for an organization."""

    case_style: dict[str, int] = field(default_factory=dict)  # kebab-case, snake_case, etc.
    prefixes: dict[str, int] = field(default_factory=dict)  # env-, team-, etc.
    separators: dict[str, int] = field(default_factory=dict)  # -, _, etc.
    total_resources: int = 0
    consistency_score: float = 100.0  # 0-100
    dominant_pattern: str = "mixed"  # Detected primary pattern
    violations: list[dict[str, Any]] = field(default_factory=list)  # Resources breaking pattern

    def get_case_distribution_percent(self) -> dict[str, float]:
        """Get percentage distribution of case styles."""
        if self.total_resources == 0:
            return {}
        return {
            case: (count / self.total_resources) * 100 for case, count in self.case_style.items()
        }

    def get_prefix_distribution_percent(self) -> dict[str, float]:
        """Get percentage distribution of prefixes."""
        if self.total_resources == 0:
            return {}
        return {
            prefix: (count / self.total_resources) * 100 for prefix, count in self.prefixes.items()
        }


@dataclass
class QualityReport:
    """Quality analysis report for an organization."""

    org_name: str
    duplicate_count: int
    duplicates: list[DuplicateResource] = field(default_factory=list)
    quality_score: float = 100.0  # 0-100, decreases with issues
    naming_pattern: NamingPattern | None = None  # Naming convention analysis

    def get_severity_counts(self) -> dict[str, int]:
        """Get count of duplicates by severity."""
        counts = {"error": 0, "warning": 0, "info": 0}
        for dup in self.duplicates:
            counts[dup.severity] = counts.get(dup.severity, 0) + 1
        return counts

    def get_duplicates_by_type(self) -> dict[str, list[DuplicateResource]]:
        """Group duplicates by resource type."""
        by_type: dict[str, list[DuplicateResource]] = {}
        for dup in self.duplicates:
            if dup.resource_type not in by_type:
                by_type[dup.resource_type] = []
            by_type[dup.resource_type].append(dup)
        return by_type


def detect_duplicates(
    resources: dict[str, list[dict[str, Any]]], org_name: str
) -> list[DuplicateResource]:
    """Detect duplicate resources within an organization.

    Args:
        resources: Resources grouped by type
        org_name: Organization name

    Returns:
        List of DuplicateResource objects
    """
    duplicates = []

    # Resource types to check for duplicates
    check_types = [
        "job_templates",
        "workflow_job_templates",
        "inventories",
        "projects",
        "credentials",
        "execution_environments",
    ]

    for resource_type in check_types:
        if resource_type not in resources:
            continue

        resource_list = resources[resource_type]

        # Group by name (case-insensitive)
        name_groups: dict[str, list[dict[str, Any]]] = {}
        for resource in resource_list:
            name = resource.get("name", "").lower().strip()
            if not name:
                continue

            if name not in name_groups:
                name_groups[name] = []
            name_groups[name].append(resource)

        # Find duplicates (same name appearing multiple times)
        for name, group in name_groups.items():
            if len(group) < 2:
                continue

            # Determine severity and recommendation
            count = len(group)
            if count >= 3:
                severity = "error"
                impact = f"HIGH - {count} copies will cause migration conflicts"
            elif count == 2:
                severity = "warning"
                impact = "MEDIUM - Creates confusion and potential conflicts"
            else:
                severity = "info"
                impact = "LOW - Informational"

            # Generate recommendation based on resource type
            original_name = group[0].get("name", name)
            if resource_type == "job_templates":
                recommendation = (
                    f"Consolidate or add environment prefix:\n"
                    f"  • prod-{original_name}\n"
                    f"  • dev-{original_name}\n"
                    f"  • test-{original_name}"
                )
            elif resource_type == "workflow_job_templates":
                recommendation = (
                    f"Add purpose or schedule identifier:\n"
                    f"  • {original_name}-nightly\n"
                    f"  • {original_name}-weekly\n"
                    f"  • {original_name}-ondemand"
                )
            elif resource_type == "inventories":
                recommendation = (
                    f"Add environment or datacenter prefix:\n"
                    f"  • prod-{original_name}\n"
                    f"  • staging-{original_name}\n"
                    f"  • us-east-{original_name}"
                )
            else:
                recommendation = "Consolidate duplicates or add differentiating prefix/suffix"

            duplicate = DuplicateResource(
                name=original_name,
                resource_type=resource_type,
                count=count,
                ids=[r.get("id") for r in group if r.get("id")],
                severity=severity,
                impact=impact,
                recommendation=recommendation,
                details=group,
            )
            duplicates.append(duplicate)

    return duplicates


def calculate_quality_score(duplicates: list[DuplicateResource]) -> float:
    """Calculate quality score based on duplicate issues.

    Args:
        duplicates: List of duplicate resources

    Returns:
        Quality score (0-100), where 100 is perfect
    """
    if not duplicates:
        return 100.0

    # Penalty by severity
    penalties = {"error": 10, "warning": 5, "info": 2}

    total_penalty = 0
    for dup in duplicates:
        penalty = penalties.get(dup.severity, 0)
        # Multiply by number of duplicates (more copies = worse)
        total_penalty += penalty * (dup.count - 1)

    # Score starts at 100, subtract penalties
    score = max(0.0, 100.0 - total_penalty)

    return round(score, 1)


def detect_case_style(name: str) -> str:
    """Detect the case style of a resource name.

    Args:
        name: Resource name to analyze

    Returns:
        Case style: kebab-case, snake_case, PascalCase, camelCase, UPPER_CASE, or mixed
    """
    if not name:
        return "unknown"

    # Check for different patterns
    has_hyphen = "-" in name
    has_underscore = "_" in name
    has_space = " " in name
    has_upper = any(c.isupper() for c in name)
    has_lower = any(c.islower() for c in name)
    is_all_upper = name.replace("_", "").replace("-", "").isupper()
    is_all_lower = name.replace("_", "").replace("-", "").islower()

    # Determine style
    if has_space:
        return "mixed"  # Spaces are non-standard
    elif is_all_upper and has_underscore:
        return "UPPER_CASE"
    elif has_hyphen and is_all_lower:
        return "kebab-case"
    elif has_underscore and is_all_lower:
        return "snake_case"
    elif has_upper and has_lower and not has_hyphen and not has_underscore:
        # Check for PascalCase vs camelCase
        if name[0].isupper():
            return "PascalCase"
        else:
            return "camelCase"
    elif is_all_lower and not has_hyphen and not has_underscore:
        return "lowercase"
    else:
        return "mixed"


def detect_prefix(name: str) -> str | None:
    """Detect common prefix pattern in resource name.

    Args:
        name: Resource name to analyze

    Returns:
        Detected prefix or None
    """
    # Common prefix patterns
    env_prefixes = ["prod-", "dev-", "test-", "staging-", "qa-", "uat-"]
    team_prefixes = ["eng-", "ops-", "devops-", "platform-", "data-", "ml-", "security-"]
    region_prefixes = [
        "us-east-",
        "us-west-",
        "eu-",
        "asia-",
        "global-",
        "local-",
    ]

    name_lower = name.lower()

    # Check for environment prefixes
    for prefix in env_prefixes:
        if name_lower.startswith(prefix):
            return f"env:{prefix}"

    # Check for team prefixes
    for prefix in team_prefixes:
        if name_lower.startswith(prefix):
            return f"team:{prefix}"

    # Check for region prefixes
    for prefix in region_prefixes:
        if name_lower.startswith(prefix):
            return f"region:{prefix}"

    # Check for generic prefix pattern (word followed by hyphen or underscore)
    match = re.match(r"^([a-z0-9]+)[-_]", name_lower)
    if match:
        prefix_word = match.group(1)
        if len(prefix_word) <= 10:  # Reasonable prefix length
            return f"custom:{prefix_word}-"

    return None


def analyze_naming_patterns(resources: dict[str, list[dict[str, Any]]]) -> NamingPattern:
    """Analyze naming patterns across resources.

    Args:
        resources: Resources grouped by type

    Returns:
        NamingPattern with statistics
    """
    case_counts = Counter()
    prefix_counts = Counter()
    separator_counts = Counter()
    total_count = 0
    violations = []

    # Resource types to analyze
    analyze_types = [
        "job_templates",
        "workflow_job_templates",
        "inventories",
        "projects",
    ]

    for resource_type in analyze_types:
        if resource_type not in resources:
            continue

        for resource in resources[resource_type]:
            name = resource.get("name", "")
            if not name:
                continue

            total_count += 1

            # Detect case style
            case_style = detect_case_style(name)
            case_counts[case_style] += 1

            # Detect prefix
            prefix = detect_prefix(name)
            if prefix:
                prefix_counts[prefix] += 1
            else:
                prefix_counts["no-prefix"] += 1

            # Detect separator
            if "-" in name:
                separator_counts["hyphen"] += 1
            elif "_" in name:
                separator_counts["underscore"] += 1
            else:
                separator_counts["none"] += 1

    # Calculate consistency score
    if total_count == 0:
        consistency_score = 100.0
        dominant_pattern = "none"
    else:
        # Dominant case style
        dominant_case = case_counts.most_common(1)[0] if case_counts else ("mixed", 0)
        case_consistency = (dominant_case[1] / total_count) * 100

        # Dominant separator
        dominant_sep = separator_counts.most_common(1)[0] if separator_counts else ("none", 0)
        sep_consistency = (dominant_sep[1] / total_count) * 100

        # Overall consistency (weighted average)
        consistency_score = (case_consistency * 0.6) + (sep_consistency * 0.4)
        dominant_pattern = f"{dominant_case[0]} with {dominant_sep[0]}"

        # Find violations (resources not following dominant pattern)
        for resource_type in analyze_types:
            if resource_type not in resources:
                continue

            for resource in resources[resource_type]:
                name = resource.get("name", "")
                if not name:
                    continue

                case_style = detect_case_style(name)
                if case_style != dominant_case[0]:
                    violations.append(
                        {
                            "name": name,
                            "resource_type": resource_type,
                            "current_style": case_style,
                            "expected_style": dominant_case[0],
                            "resource_id": resource.get("id"),
                        }
                    )

    return NamingPattern(
        case_style=dict(case_counts),
        prefixes=dict(prefix_counts),
        separators=dict(separator_counts),
        total_resources=total_count,
        consistency_score=round(consistency_score, 1),
        dominant_pattern=dominant_pattern,
        violations=violations[:50],  # Limit to 50 for performance
    )


def generate_quality_report(
    resources: dict[str, list[dict[str, Any]]], org_name: str
) -> QualityReport:
    """Generate quality report for an organization.

    Args:
        resources: Resources grouped by type
        org_name: Organization name

    Returns:
        QualityReport with duplicate detection and naming analysis
    """
    # Detect duplicates
    duplicates = detect_duplicates(resources, org_name)
    quality_score = calculate_quality_score(duplicates)

    # Analyze naming patterns
    naming_pattern = analyze_naming_patterns(resources)

    # Adjust quality score based on naming consistency
    # Poor naming consistency (< 60%) reduces overall quality
    if naming_pattern.consistency_score < 60:
        quality_score *= 0.9  # 10% penalty
    elif naming_pattern.consistency_score < 80:
        quality_score *= 0.95  # 5% penalty

    return QualityReport(
        org_name=org_name,
        duplicate_count=len(duplicates),
        duplicates=duplicates,
        quality_score=round(quality_score, 1),
        naming_pattern=naming_pattern,
    )
