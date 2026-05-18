from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_DIR = REPO_ROOT / "container"


def test_docker_compose_declares_expected_services_and_healthchecks() -> None:
    compose = yaml.safe_load((CONTAINER_DIR / "docker-compose.yml").read_text())
    services = compose["services"]

    assert {"db", "engine", "ui", "bridge"} <= set(services)
    assert services["engine"]["healthcheck"]["test"][0] == "CMD"
    assert services["ui"]["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8080/healthz",
    ]
    assert "AAP_TOKEN_ENCRYPTION_KEY" in services["engine"]["environment"]


def test_containerfile_ui_contains_expected_build_steps() -> None:
    text = (CONTAINER_DIR / "Containerfile.ui").read_text()

    assert "COPY web/package.json web/package-lock.json* ./" in text
    assert "RUN npm run build" in text
    assert "COPY --from=builder /build/dist /usr/share/nginx/html" in text
    assert "COPY container/nginx.conf /etc/nginx/nginx.conf" in text
    assert "COPY container/start-nginx.sh /usr/local/bin/start-nginx.sh" in text


def test_nginx_configuration_validates() -> None:
    auth_conf = Path("/etc/nginx/conf.d/basic_auth.conf")
    auth_conf.parent.mkdir(parents=True, exist_ok=True)
    auth_conf.write_text("auth_basic off;\n")
    temp_conf = Path("/tmp/nginx-test.conf")
    temp_conf.write_text(
        (CONTAINER_DIR / "nginx.conf")
        .read_text()
        .replace("http://engine:8000", "http://127.0.0.1:8000")
    )
    try:
        result = subprocess.run(
            ["nginx", "-t", "-c", str(temp_conf)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        auth_conf.unlink(missing_ok=True)
        temp_conf.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr


def test_start_nginx_script_disables_basic_auth_by_default(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nginx_calls = tmp_path / "nginx.calls"
    (fake_bin / "nginx").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" > {nginx_calls}\n")
    os.chmod(fake_bin / "nginx", 0o755)

    auth_conf = Path("/etc/nginx/conf.d/basic_auth.conf")
    auth_conf.unlink(missing_ok=True)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["BASIC_AUTH_ENABLED"] = "false"

    result = subprocess.run(
        ["sh", str(CONTAINER_DIR / "start-nginx.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert auth_conf.read_text().strip() == "auth_basic off;"
    assert nginx_calls.read_text().strip() == "-g daemon off;"


def test_start_nginx_script_requires_htpasswd_when_auth_enabled(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "nginx").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(fake_bin / "nginx", 0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["BASIC_AUTH_ENABLED"] = "true"
    env["BASIC_AUTH_USER_FILE"] = str(tmp_path / "missing.htpasswd")

    result = subprocess.run(
        ["sh", str(CONTAINER_DIR / "start-nginx.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "no htpasswd file was found" in result.stderr


def test_start_nginx_script_writes_basic_auth_config(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "nginx").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(fake_bin / "nginx", 0o755)

    htpasswd = tmp_path / ".htpasswd"
    htpasswd.write_text("user:$apr1$hash\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["BASIC_AUTH_ENABLED"] = "yes"
    env["BASIC_AUTH_REALM"] = "Protected Area"
    env["BASIC_AUTH_USER_FILE"] = str(htpasswd)

    result = subprocess.run(
        ["sh", str(CONTAINER_DIR / "start-nginx.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    auth_conf = Path("/etc/nginx/conf.d/basic_auth.conf")
    assert result.returncode == 0, result.stderr
    contents = auth_conf.read_text()
    assert 'auth_basic "Protected Area";' in contents
    assert f"auth_basic_user_file {htpasswd};" in contents
