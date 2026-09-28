import argparse
import os
import sys

import pytest

import cli
from config.config import reload_config

_ENV_KEYS = (
    "CORTEX_MCP_PAPI_AUTH_ID",
    "CORTEX_MCP_PAPI_AUTH_HEADER",
    "CORTEX_MCP_PAPI_URL",
    "LOG_LEVEL",
    "CORTEX_MCP_UPDATE_FOLDER",
)


@pytest.fixture
def isolated_env():
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    cli.config = reload_config()


def _parse(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["mcp_cli", *argv])
    return cli.parse_args()


def test_parse_start_update_and_version(monkeypatch):
    start = _parse(
        monkeypatch,
        "start",
        "--api_key_id",
        "7",
        "--api_key_secret",
        "secret",
        "--server-url",
        "https://tenant.example",
        "--log-level",
        "INFO",
    )
    assert start.command == "start"
    assert start.func is cli.start_server
    assert start.api_key_id == 7
    assert start.api_key_secret == "secret"
    assert start.server_url == "https://tenant.example"
    assert start.log_level == "INFO"

    update = _parse(
        monkeypatch,
        "update",
        "--api_key_id",
        "3",
        "--api_key_secret",
        "secret",
        "--server-url",
        "https://tenant.example",
        "--folder",
        "/tmp/remote-tools",
    )
    assert update.command == "update"
    assert update.func is cli.update_tools
    assert update.folder == "/tmp/remote-tools"

    version = _parse(monkeypatch, "version")
    assert version.command == "version"
    assert version.func is cli.display_version


def test_parse_start_defaults_log_level_and_rejects_unknown_input(monkeypatch):
    start = _parse(
        monkeypatch,
        "start",
        "--api_key_id",
        "1",
        "--api_key_secret",
        "secret",
        "--server-url",
        "https://tenant.example",
    )
    assert start.log_level == "DEBUG"

    with pytest.raises(SystemExit) as missing:
        _parse(monkeypatch)
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as bad_level:
        _parse(
            monkeypatch,
            "start",
            "--api_key_id",
            "1",
            "--api_key_secret",
            "secret",
            "--server-url",
            "https://tenant.example",
            "--log-level",
            "VERBOSE",
        )
    assert bad_level.value.code == 2


def test_setup_env_exports_settings(isolated_env):
    cli.setup_env(
        argparse.Namespace(
            api_key_id=9,
            api_key_secret="header-secret",
            server_url="https://api.example",
            log_level="WARNING",
            folder="/var/remote-tools",
        )
    )

    assert os.environ["CORTEX_MCP_PAPI_AUTH_ID"] == "9"
    assert os.environ["CORTEX_MCP_PAPI_AUTH_HEADER"] == "header-secret"
    assert os.environ["CORTEX_MCP_PAPI_URL"] == "https://api.example"
    assert os.environ["LOG_LEVEL"] == "WARNING"
    assert os.environ["CORTEX_MCP_UPDATE_FOLDER"] == "/var/remote-tools"
    assert cli.config.papi_auth_id_key == "9"
    assert cli.config.papi_auth_header_key == "header-secret"
    assert cli.config.papi_url_env_key == "https://api.example"
    assert cli.config.log_level == "WARNING"
    assert cli.config.update_folder == "/var/remote-tools"


def test_setup_env_ignores_commands_without_log_level_or_folder(isolated_env):
    os.environ.pop("LOG_LEVEL", None)
    os.environ.pop("CORTEX_MCP_UPDATE_FOLDER", None)
    cli.setup_env(argparse.Namespace(api_key_id=4, api_key_secret="secret", server_url="https://tenant.example"))
    assert os.environ["CORTEX_MCP_PAPI_AUTH_ID"] == "4"
    assert "LOG_LEVEL" not in os.environ
    assert "CORTEX_MCP_UPDATE_FOLDER" not in os.environ


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("api_key_id", "API key ID is required"),
        ("api_key_secret", "API key is required"),
        ("server_url", "PAPI Server URL is required"),
    ],
)
def test_setup_env_exits_when_a_required_value_is_missing(isolated_env, caplog, field, message):
    values = {"api_key_id": 1, "api_key_secret": "secret", "server_url": "https://tenant.example"}
    values[field] = None
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit) as caught:
            cli.setup_env(argparse.Namespace(**values))
    assert caught.value.code == 1
    assert message in caplog.text
    assert ("CORTEX_MCP_PAPI_AUTH_ID" in os.environ) is (field != "api_key_id")
    assert ("CORTEX_MCP_PAPI_AUTH_HEADER" in os.environ) is (field == "server_url")
    assert "CORTEX_MCP_PAPI_URL" not in os.environ
