"""Guard that every Settings field is documented in README.md and .env.example (issue #54).

Config docs drifted twice before this guard existed: MCP_MAX_RESPONSE_BYTES
shipped undocumented in both files, and CORTEX_MCP_UPDATE_FOLDER was missing
from the README. The direction is Settings -> docs only: every validation
alias on ``config.config.Settings`` must appear in BOTH files. Entries in
.env.example that are explicitly not read by this server (e.g.
MCP_REQUEST_TIMEOUT_MS, a gateway/client setting) are not Settings fields and
are therefore out of scope.

The alias list is derived from the model itself, not a hand-maintained copy,
so a new Settings field cannot ship undocumented: the parametrized test below
fails naming the missing alias and the file that lacks it. Same doctrine as
the README tool-count guard in tests/test_server_registration.py (issue #48):
pin the measured source of truth against the published docs.
"""

import re
from pathlib import Path

import pytest

from config.config import Settings

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"


def _settings_aliases() -> list[str]:
    """Every Settings field's environment-variable name, from the model itself."""
    aliases: list[str] = []
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if not isinstance(alias, str):
            raise AssertionError(
                f"Settings.{name} has a non-string validation_alias ({alias!r}). "
                "Extend _settings_aliases() in tests/test_config_docs.py to resolve it "
                "so this guard keeps covering every config field."
            )
        aliases.append(alias)
    assert aliases, "Settings has no fields; the guard would pass vacuously"
    assert len(aliases) == len(set(aliases)), "duplicate validation aliases in Settings"
    return sorted(aliases)


@pytest.mark.parametrize("doc", [README, ENV_EXAMPLE], ids=["README.md", ".env.example"])
@pytest.mark.parametrize("alias", _settings_aliases())
def test_settings_alias_is_documented(alias: str, doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    # Word boundaries so MCP_PORT cannot pass by hiding inside a longer name.
    assert re.search(rf"\b{re.escape(alias)}\b", text), (
        f"{alias} is a Settings validation alias but is missing from {doc.name}. "
        f"Document it there: add a row to the README configuration tables and an "
        f"entry (commented out if optional) to .env.example, with its default and behavior."
    )


def test_log_level_default_matches_docs() -> None:
    """Regression for #56: the presence guard above let default *values* drift.

    Settings said DEBUG while README and .env.example said INFO. Pin LOG_LEVEL's
    documented default to the model's. The CLI --log-level help text derives its
    fallback from the same Settings field, so all four surfaces share one source
    of truth.
    """
    default = Settings.model_fields["log_level"].default

    readme = README.read_text(encoding="utf-8")
    row = re.search(r"^\|\s*`LOG_LEVEL`\s*\|\s*`([^`]+)`\s*\|", readme, re.MULTILINE)
    assert row, "README.md has no LOG_LEVEL configuration-table row to check the default against"
    assert row.group(1) == default, (
        f"README.md documents LOG_LEVEL default {row.group(1)!r} but Settings.log_level "
        f"defaults to {default!r}. Make them agree (#56)."
    )

    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    entry = re.search(r"^LOG_LEVEL=(\S+)$", env_example, re.MULTILINE)
    assert entry, ".env.example has no active LOG_LEVEL entry to check the default against"
    assert entry.group(1) == default, (
        f".env.example sets LOG_LEVEL={entry.group(1)!r} but Settings.log_level "
        f"defaults to {default!r}. Make them agree (#56)."
    )
