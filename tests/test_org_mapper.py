"""Tests for organization-scoped failure mapping in reports."""

from __future__ import annotations

import json
from pathlib import Path

from aap_migration.reporting.org_mapper import OrganizationMapper


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_org_mapper_resolves_org_scoped_and_global_resources(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"

    _write_json(
        export_dir / "organizations/org.json",
        [{"id": 1, "name": "Default"}],
    )
    _write_json(
        export_dir / "projects/projects.json",
        [{"id": 10, "name": "App", "organization": 1}],
    )

    mapper = OrganizationMapper(export_dir, transform_dir)

    assert mapper.get_organization_name("organizations", 1) == "(Global)"
    assert mapper.get_organization_name("projects", 10) == "Default"
    assert mapper.get_organization_name("projects", 999) == "(Unknown)"


def test_org_mapper_build_org_summary_groups_failures(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"

    _write_json(
        export_dir / "organizations/org.json",
        [{"id": 1, "name": "Default"}],
    )
    _write_json(
        export_dir / "job_templates/jt.json",
        [{"id": 5, "name": "Demo JT", "organization": 1}],
    )

    mapper = OrganizationMapper(export_dir, transform_dir)
    summary = mapper.build_org_summary(
        [
            {
                "resource_type": "job_templates",
                "source_id": 5,
                "source_name": "Demo JT",
                "status": "failed",
                "error_message": "boom",
            },
            {
                "resource_type": "credential_types",
                "source_id": 1,
                "source_name": "ssh",
                "status": "skipped",
            },
        ]
    )

    assert summary["Default"]["failed"] == 1
    assert summary["Default"]["total"] == 1
    assert "job_templates" in summary["Default"]["resource_types"]
    assert summary["(Global)"]["skipped"] == 1


def test_org_mapper_resolves_schedule_org_via_job_template(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"

    _write_json(
        export_dir / "organizations/org.json",
        [{"id": 1, "name": "Default"}],
    )
    _write_json(
        export_dir / "job_templates/jt.json",
        [{"id": 9, "name": "Nightly", "organization": 1}],
    )
    _write_json(
        export_dir / "schedules/schedules.json",
        [{"id": 50, "name": "Daily", "unified_job_template": 9}],
    )

    mapper = OrganizationMapper(export_dir, transform_dir)
    assert mapper.get_organization_name("schedules", 50) == "Default"


def test_org_mapper_parent_scoped_host_uses_summary_fields(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"

    _write_json(
        export_dir / "organizations/org.json",
        [{"id": 1, "name": "Default"}],
    )
    _write_json(
        export_dir / "hosts/hosts.json",
        [
            {
                "id": 7,
                "name": "host1",
                "summary_fields": {"organization": {"id": 1, "name": "Default"}},
            }
        ],
    )

    mapper = OrganizationMapper(export_dir, transform_dir)
    assert mapper.get_organization_name("hosts", 7) == "Default"


def test_org_mapper_handles_missing_organization_export(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    transform_dir = tmp_path / "xformed"
    mapper = OrganizationMapper(export_dir, transform_dir)
    assert mapper.org_names == {}
