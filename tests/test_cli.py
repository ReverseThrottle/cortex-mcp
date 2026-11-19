import argparse
import io
import os
import stat
import sys
import zipfile

import pytest

import cli
import main
from config.config import reload_config
from entities.exceptions import PAPIAuthenticationError

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
    # Parser defaults are read from cli.config at parse_args() time; reload so
    # they reflect the isolated environment, not whatever was set at import.
    cli.config = reload_config()
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


def _args(**extra) -> argparse.Namespace:
    return argparse.Namespace(api_key_id=1, api_key_secret="secret", server_url="https://tenant.example", **extra)


def _zip(*members: str | zipfile.ZipInfo) -> zipfile.ZipFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member in members:
            archive.writestr(member, "data")
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


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


def test_parse_start_log_level_defaults_to_none_and_rejects_unknown_input(monkeypatch):
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
    # None means "not passed": setup_env must leave LOG_LEVEL from env/.env
    # alone instead of exporting a parser default over it (#56).
    assert start.log_level is None

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
    ("flag_level", "effective"),
    [(None, "WARNING"), ("ERROR", "ERROR")],
    ids=["omitted-flag-preserves-env", "explicit-flag-overrides-env"],
)
def test_log_level_flag_env_precedence(isolated_env, monkeypatch, flag_level, effective):
    """Regression for #56: omitting --log-level must not clobber LOG_LEVEL.

    Goes through the real parser and setup_env with LOG_LEVEL=WARNING in the
    environment: omitted flag keeps WARNING, explicit flag wins over it.
    """
    os.environ["LOG_LEVEL"] = "WARNING"
    argv = ["start", "--api_key_id", "1", "--api_key_secret", "secret", "--server-url", "https://tenant.example"]
    if flag_level is not None:
        argv += ["--log-level", flag_level]
    args = _parse(monkeypatch, *argv)
    cli.setup_env(args)
    assert os.environ["LOG_LEVEL"] == effective
    assert cli.config.log_level == effective


def test_log_level_unset_everywhere_falls_back_to_settings_default(isolated_env, monkeypatch):
    """With no flag and no LOG_LEVEL in the environment, Settings supplies INFO."""
    args = _parse(
        monkeypatch,
        "start",
        "--api_key_id",
        "1",
        "--api_key_secret",
        "secret",
        "--server-url",
        "https://tenant.example",
    )
    cli.setup_env(args)
    assert "LOG_LEVEL" not in os.environ
    assert cli.config.log_level == "INFO"


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


@pytest.mark.parametrize("command", ["start", "update"])
def test_omitted_api_key_id_with_unset_env_reaches_guidance(isolated_env, monkeypatch, caplog, command):
    """Regression for #47: the empty-string env default must not crash argparse.

    Goes through the real parser with CORTEX_MCP_PAPI_AUTH_ID unset, then
    setup_env, expecting the friendly exit-1 guidance instead of exit 2.
    """
    args = _parse(monkeypatch, command, "--api_key_secret", "secret", "--server-url", "https://tenant.example")
    assert args.api_key_id is None
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit) as caught:
            cli.setup_env(args)
    assert caught.value.code == 1
    assert "API key ID is required. Please provide --api_key_id or set CORTEX_MCP_PAPI_AUTH_ID" in caplog.text


@pytest.mark.parametrize(
    ("flag", "guidance"),
    [
        ("--api_key_id", "Please provide --api_key_id or set CORTEX_MCP_PAPI_AUTH_ID"),
        ("--api_key_secret", "Please provide --api_key_secret or set CORTEX_MCP_PAPI_AUTH_HEADER"),
        ("--server-url", "Please provide --server-url or set CORTEX_MCP_PAPI_URL"),
    ],
)
def test_missing_value_guidance_names_flags_the_parser_accepts(isolated_env, monkeypatch, caplog, flag, guidance):
    """Regression for #47: guidance must name real flags (e.g. --server-url, not --server_url)."""
    pairs = [("--api_key_id", "1"), ("--api_key_secret", "secret"), ("--server-url", "https://tenant.example")]
    argv = ["start"] + [token for name, value in pairs if name != flag for token in (name, value)]
    args = _parse(monkeypatch, *argv)
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit) as caught:
            cli.setup_env(args)
    assert caught.value.code == 1
    assert guidance in caplog.text


def test_env_provided_api_key_id_still_parses_and_exports(isolated_env, monkeypatch):
    monkeypatch.setenv("CORTEX_MCP_PAPI_AUTH_ID", "7")
    cli.config = reload_config()
    args = _parse(monkeypatch, "start", "--api_key_secret", "secret", "--server-url", "https://tenant.example")
    assert args.api_key_id == 7
    cli.setup_env(args)
    assert os.environ["CORTEX_MCP_PAPI_AUTH_ID"] == "7"
    assert cli.config.papi_auth_id_key == "7"


def test_explicit_non_integer_api_key_id_is_still_an_argparse_error(isolated_env, monkeypatch):
    with pytest.raises(SystemExit) as caught:
        _parse(monkeypatch, "start", "--api_key_id", "not-a-number")
    assert caught.value.code == 2


@pytest.mark.parametrize("name", ["../evil.txt", "remote_tools/../../evil.txt", "/tmp/evil.txt"])
def test_safe_extract_rejects_members_outside_the_destination(tmp_path, name):
    target = tmp_path / "out"
    with pytest.raises(ValueError, match="Unsafe path"):
        cli.safe_extract(_zip("remote_tools/ok.yml", name), str(target))
    assert not target.exists()


def test_safe_extract_rejects_symlink_entries(tmp_path):
    link = zipfile.ZipInfo("remote_tools/link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="symlink"):
        cli.safe_extract(_zip("remote_tools/ok.yml", link), str(tmp_path))
    assert not (tmp_path / "remote_tools").exists()


def test_safe_extract_accepts_names_that_only_contain_dots(tmp_path):
    cli.safe_extract(_zip("remote_tools/a..b.yml", "remote_tools/v1..2/tool.yml"), str(tmp_path))
    assert (tmp_path / "remote_tools" / "a..b.yml").read_text() == "data"
    assert (tmp_path / "remote_tools" / "v1..2" / "tool.yml").read_text() == "data"


@pytest.mark.asyncio
async def test_start_server_exits_non_zero_when_the_server_fails(isolated_env, monkeypatch, caplog):
    async def broken(transport):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(cli, "async_main", broken)
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit) as caught:
            await cli.start_server(_args(log_level="INFO"))
    assert caught.value.code == 1
    assert "bind failed" in caplog.text


def test_main_exits_non_zero_when_the_server_fails(monkeypatch):
    async def broken(transport):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(main, "async_main", broken)
    with pytest.raises(SystemExit) as caught:
        main.main()
    assert caught.value.code == 1


@pytest.mark.asyncio
async def test_update_reports_papi_download_errors(isolated_env, monkeypatch, tmp_path, caplog):
    async def denied():
        raise PAPIAuthenticationError("Authentication failed (HTTP 401): bad key", status_code=401)

    monkeypatch.setattr(cli, "download_update_package", denied)
    with caplog.at_level("ERROR"):
        await cli.update_tools(_args(folder=str(tmp_path)))
    assert "Failed to download content from API: Authentication failed" in caplog.text
    assert "HTTP Status Code: 401" in caplog.text
    assert "Unexpected error" not in caplog.text
