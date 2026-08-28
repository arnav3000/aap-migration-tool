from __future__ import annotations

import json

from aap_migration.validation.payload_validator import PayloadValidator, create_validation_report


def test_validate_payload_skips_when_no_schema_loaded() -> None:
    validator = PayloadValidator()

    valid, errors = validator.validate_payload("organizations", {"name": "Default"})

    assert valid is True
    assert errors == []


def test_validate_payload_reports_missing_and_wrong_types(tmp_path) -> None:
    schema_file = tmp_path / "target-schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "schemas": {
                    "organizations": {
                        "fields": {
                            "name": {"required": True, "type": "string"},
                            "count": {"required": True, "type": "integer"},
                            "enabled": {"required": False, "type": "boolean"},
                            "created": {"required": True, "type": "string", "read_only": True},
                        }
                    }
                }
            }
        )
    )

    validator = PayloadValidator(schema_file)
    valid, errors = validator.validate_payload(
        "organizations",
        {"count": "not-an-int", "enabled": "yes", "_source_id": 1},
    )

    assert valid is False
    assert "Missing required field: name" in errors
    assert "Field 'count' expected integer, got str" in errors
    assert "Field 'enabled' expected boolean, got str" in errors


def test_validate_batch_and_report_generation(tmp_path) -> None:
    schema_file = tmp_path / "target-schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "schemas": {
                    "projects": {
                        "fields": {
                            "name": {"required": True, "type": "string"},
                        }
                    }
                }
            }
        )
    )
    validator = PayloadValidator(schema_file)

    result = validator.validate_batch(
        "projects",
        [{"name": "Valid"}, {"missing": "field"}],
    )

    assert result["valid_count"] == 1
    assert result["invalid_count"] == 1
    assert result["errors"][0]["resource"] == "unknown"

    report_file = tmp_path / "reports" / "validation.json"
    create_validation_report({"projects": result}, report_file)

    assert json.loads(report_file.read_text())["projects"]["invalid_count"] == 1
