import pytest
from pydantic import ValidationError

from config.config import Settings

_NAMES = ("MCP_WRITE_CONFIRMATION", "MCP_ELICITATION_ENABLED", "MCP_TOOLS_ALLOW", "MCP_TOOLS_DENY")


@pytest.fixture
def clean_env(monkeypatch):
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("mode", "legacy", "expected"),
    [
        (None, None, "client"),
        (None, "false", "client"),
        (None, "true", "require"),
        ("client", "true", "client"),
        ("require", "false", "require"),
        ("Elicit", None, "elicit"),
        (" require ", None, "require"),
        ("", None, "client"),
        ("", "true", "require"),
    ],
)
def test_effective_write_confirmation(clean_env, mode, legacy, expected):
    if mode is not None:
        clean_env.setenv("MCP_WRITE_CONFIRMATION", mode)
    if legacy is not None:
        clean_env.setenv("MCP_ELICITATION_ENABLED", legacy)
    assert Settings().effective_write_confirmation == expected


def test_unknown_mode_is_rejected(clean_env):
    clean_env.setenv("MCP_WRITE_CONFIRMATION", "sometimes")
    with pytest.raises(ValidationError):
        Settings()


def test_deprecated_variable_is_detectable_even_when_false(clean_env):
    assert "elicitation_enabled" not in Settings().model_fields_set
    clean_env.setenv("MCP_ELICITATION_ENABLED", "false")
    assert "elicitation_enabled" in Settings().model_fields_set


def test_tool_lists_are_trimmed_sets(clean_env):
    clean_env.setenv("MCP_TOOLS_ALLOW", " update_case, run_xql_query ,,")
    clean_env.setenv("MCP_TOOLS_DENY", "post_lists_delete")
    settings = Settings()
    assert settings.tools_allow_set == frozenset({"update_case", "run_xql_query"})
    assert settings.tools_deny_set == frozenset({"post_lists_delete"})


def test_tool_lists_default_to_empty(clean_env):
    settings = Settings()
    assert settings.tools_allow_set == frozenset()
    assert settings.tools_deny_set == frozenset()
