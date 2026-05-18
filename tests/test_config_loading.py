from __future__ import annotations

import pytest
import yaml

from aap_migration.config import (
    AAPInstanceConfig,
    LoggingConfig,
    MigrationConfig,
    PerformanceConfig,
    load_config_from_yaml,
    save_config_to_yaml,
)


def test_aap_instance_and_logging_validators() -> None:
    cfg = AAPInstanceConfig(url="https://example.com/", token="abc")
    assert cfg.url == "https://example.com"

    with pytest.raises(ValueError, match="must start with http:// or https://"):
        AAPInstanceConfig(url="example.com", token="abc")

    with pytest.raises(ValueError, match="should use HTTPS"):
        AAPInstanceConfig(url="http://example.com", token="abc")

    with pytest.raises(ValueError, match="Token cannot be empty"):
        AAPInstanceConfig(url="https://example.com", token=" ")

    assert LoggingConfig(level="debug", file_level="warning", format="console").level == "DEBUG"

    with pytest.raises(ValueError, match="Log level must be one of"):
        LoggingConfig(level="verbose")

    with pytest.raises(ValueError, match="Log format must be one of"):
        LoggingConfig(format="xml")


def test_performance_validators() -> None:
    cfg = PerformanceConfig()
    assert cfg.batch_sizes["hosts"] == 200

    with pytest.raises(ValueError, match="Host batch size cannot exceed 200"):
        PerformanceConfig(batch_sizes={"hosts": 201})

    with pytest.raises(ValueError, match="less than or equal to 25"):
        PerformanceConfig(cleanup_job_cancel_concurrency=26)


def test_load_config_from_yaml_expands_env_and_loads_external_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "mappings.yaml").write_text(
        yaml.safe_dump({"organizations": {"Default": "Renamed Default"}})
    )
    (config_dir / "ignored_endpoints.yaml").write_text(
        yaml.safe_dump({"ignored_endpoints": {"common": ["ping"], "source": ["metrics"]}})
    )
    config_path = tmp_path / "migration.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "source": {"url": "https://source.example.com", "token": "${SOURCE_TOKEN}"},
                "target": {"url": "https://target.example.com", "token": "target-token"},
                "paths": {
                    "mappings_file": "config/mappings.yaml",
                    "ignored_endpoints_file": "config/ignored_endpoints.yaml",
                },
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SOURCE_TOKEN", "source-token")

    config = load_config_from_yaml(config_path)

    assert config.source.token == "source-token"
    assert config.resource_mappings == {"organizations": {"Default": "Renamed Default"}}
    assert config.ignored_endpoints == {
        "common": ["ping"],
        "source": ["metrics"],
        "target": [],
    }


def test_load_config_from_yaml_supports_legacy_ignored_endpoint_lists(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ignored_endpoints.yaml").write_text(
        yaml.safe_dump({"ignored_endpoints": ["ping", "metrics"]})
    )
    config_path = tmp_path / "migration.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "source": {"url": "https://source.example.com", "token": "source-token"},
                "target": {"url": "https://target.example.com", "token": "target-token"},
                "paths": {"ignored_endpoints_file": "config/ignored_endpoints.yaml"},
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    config = load_config_from_yaml(config_path)

    assert config.ignored_endpoints == {
        "common": ["ping", "metrics"],
        "source": [],
        "target": [],
    }


def test_load_config_from_yaml_errors_for_missing_files_and_env(tmp_path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError):
        load_config_from_yaml(missing)

    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(ValueError, match="Empty configuration file"):
        load_config_from_yaml(empty)

    env_missing = tmp_path / "env-missing.yaml"
    env_missing.write_text(
        yaml.safe_dump(
            {
                "source": {"url": "https://source.example.com", "token": "${NOPE}"},
                "target": {"url": "https://target.example.com", "token": "target-token"},
            }
        )
    )
    with pytest.raises(ValueError, match="Environment variable 'NOPE' not found"):
        load_config_from_yaml(env_missing)


def test_save_config_to_yaml_round_trips(tmp_path) -> None:
    config = MigrationConfig(
        source={"url": "https://source.example.com", "token": "source-token"},
        target={"url": "https://target.example.com", "token": "target-token"},
    )
    output = tmp_path / "nested" / "saved.yaml"

    save_config_to_yaml(config, output)

    saved = yaml.safe_load(output.read_text())
    assert saved["source"]["url"] == "https://source.example.com"
    assert saved["target"]["token"] == "target-token"
