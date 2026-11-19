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
