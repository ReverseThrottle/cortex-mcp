import logging

import pytest

from config.config import Settings
from main import resolve_transport
from pkg.startup_checks import check_write_confirmation


@pytest.fixture
def settings(monkeypatch):
    for name in ("MCP_WRITE_CONFIRMATION", "MCP_ELICITATION_ENABLED"):
        monkeypatch.delenv(name, raising=False)

    def build(mode=None, legacy=None) -> Settings:
        if mode is not None:
            monkeypatch.setenv("MCP_WRITE_CONFIRMATION", mode)
        if legacy is not None:
            monkeypatch.setenv("MCP_ELICITATION_ENABLED", legacy)
        return Settings(_env_file=None)

    return build


def test_require_on_http_stops_startup(settings):
    with pytest.raises(ValueError, match="MCP_WRITE_CONFIRMATION=require"):
        check_write_confirmation(settings("require"), "streamable-http")


def test_deprecated_true_on_http_stops_startup(settings):
    with pytest.raises(ValueError, match="MCP_WRITE_CONFIRMATION=require"):
        check_write_confirmation(settings(legacy="true"), "streamable-http")


def test_deprecated_source_message_names_the_variable_and_the_fix(settings):
    with pytest.raises(ValueError) as caught:
        check_write_confirmation(settings(legacy="true"), "streamable-http")
    message = str(caught.value)
    assert "MCP_ELICITATION_ENABLED" in message
    assert "MCP_WRITE_CONFIRMATION=client" in message
    assert "elicit" in message


def test_explicit_require_message_does_not_blame_the_deprecated_variable(settings):
    with pytest.raises(ValueError) as caught:
        check_write_confirmation(settings("require"), "streamable-http")
    assert "MCP_ELICITATION_ENABLED" not in str(caught.value)


def test_explicit_require_with_deprecated_true_is_not_blamed_on_the_deprecated_variable(settings):
    with pytest.raises(ValueError) as caught:
        check_write_confirmation(settings("require", legacy="true"), "streamable-http")
    assert "MCP_ELICITATION_ENABLED" not in str(caught.value)


def test_require_on_stdio_is_allowed(settings):
    check_write_confirmation(settings("require"), "stdio")


def test_elicit_on_http_warns_but_starts(settings, caplog):
    with caplog.at_level(logging.WARNING, logger="Cortex MCP"):
        check_write_confirmation(settings("elicit"), "streamable-http")
    assert "cannot be satisfied" in caplog.text


def test_client_mode_is_silent(settings, caplog):
    with caplog.at_level(logging.WARNING, logger="Cortex MCP"):
        check_write_confirmation(settings("client"), "streamable-http")
    assert caplog.text == ""


def test_deprecated_variable_warns_even_when_false(settings, caplog):
    with caplog.at_level(logging.WARNING, logger="Cortex MCP"):
        check_write_confirmation(settings(legacy="false"), "streamable-http")
    assert "MCP_ELICITATION_ENABLED is deprecated" in caplog.text


def test_elicit_on_stdio_is_silent_and_allowed(settings, caplog):
    with caplog.at_level(logging.DEBUG, logger="Cortex MCP"):
        check_write_confirmation(settings("elicit"), "stdio")
    assert caplog.text == ""


@pytest.mark.parametrize("legacy", ["true", "false"])
def test_write_confirmation_wins_over_the_deprecated_variable_on_stdio(settings, caplog, legacy):
    config = settings("require", legacy=legacy)
    assert config.effective_write_confirmation == "require"
    with caplog.at_level(logging.WARNING, logger="Cortex MCP"):
        check_write_confirmation(config, "stdio")
    assert "MCP_ELICITATION_ENABLED is deprecated" in caplog.text
    assert "Current resolved mode: require" in caplog.text


@pytest.mark.parametrize("legacy", ["true", "false"])
def test_require_with_the_deprecated_variable_still_stops_http_startup(settings, legacy):
    with pytest.raises(ValueError, match="MCP_WRITE_CONFIRMATION=require"):
        check_write_confirmation(settings("require", legacy=legacy), "streamable-http")


def test_explicit_client_mode_overrides_the_deprecated_true_on_http(settings, caplog):
    config = settings("client", legacy="true")
    assert config.effective_write_confirmation == "client"
    with caplog.at_level(logging.WARNING, logger="Cortex MCP"):
        check_write_confirmation(config, "streamable-http")
    assert "MCP_ELICITATION_ENABLED is deprecated" in caplog.text
    assert "cannot be satisfied" not in caplog.text


def test_the_check_only_guards_the_resolved_stateless_transport(settings):
    # The check takes the resolved FastMCP name. An unrecognized string is not
    # "streamable-http", so the check does nothing; resolve_transport rejects it.
    check_write_confirmation(settings("require"), "websocket")
    check_write_confirmation(settings("require"), "http")  # unresolved alias is not checked either


@pytest.mark.parametrize("transport", ["websocket", "sse", ""])
def test_an_unrecognized_transport_is_rejected_before_any_check(transport):
    with pytest.raises(ValueError, match="not supported"):
        resolve_transport(transport)
