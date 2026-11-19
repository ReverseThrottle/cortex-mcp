"""Guard the per-flag tool registration totals published in README.md (issue #48).

The README's "Public API catalog" section states how many tools the server
registers for four flag combinations. These tests build the real server
offline with dummy credentials (same pattern as test_public_api_catalog.py)
and pin both the measured counts and the README text to one constant table,
so neither side can drift silently.
"""

import os
import re
from pathlib import Path

import pytest

from config.config import reload_config
from main import initialize_mcp_server

README = Path(__file__).resolve().parents[1] / "README.md"

FLAG_NAMES = (
    "MCP_WRITE_TOOLS_ENABLED",
    "MCP_SECRET_TOOLS_ENABLED",
    "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED",
)

# (write, secret, isolate) -> registered tool count, measured against the real
# server. On a legitimate count change, update this table AND the README.md
# totals sentence located by _readme_totals_line() below.
EXPECTED_TOOL_COUNTS: dict[tuple[bool, bool, bool], int] = {
    (False, False, False): 255,
    (True, False, False): 523,
    (True, False, True): 525,
    (True, True, True): 530,
}

# How the README states each total, keyed by the same flag tuples.
README_TOTAL_PATTERNS: dict[tuple[bool, bool, bool], str] = {
    (False, False, False): r"flags off, the server registers (\d+) tools",
    (True, False, False): r"only write tools enabled, it registers (\d+)",
    (True, False, True): r"write and isolate flags enabled, it registers (\d+)",
    (True, True, True): r"every tool \((\d+)\) registered when write, secret, and isolate tools are all enabled",
}


def _update_sites(flags: tuple[bool, bool, bool]) -> str:
    lineno, _ = _readme_totals_line()
    return (
        f"If the new count for (write, secret, isolate)={flags} is legitimate, update "
        f"EXPECTED_TOOL_COUNTS in tests/test_server_registration.py and the totals "
        f"sentence at README.md:{lineno}."
    )


def _readme_totals_line() -> tuple[int, str]:
    for lineno, line in enumerate(README.read_text(encoding="utf-8").splitlines(), start=1):
        if "the server registers" in line and "all enabled" in line:
            return lineno, line
    pytest.fail(
        "README.md no longer contains the per-flag totals sentence ('the server registers ... all enabled'). "
        "Restore it or update README_TOTAL_PATTERNS and EXPECTED_TOOL_COUNTS in tests/test_server_registration.py."
    )


@pytest.fixture
def feature_flags(monkeypatch):
    previous = {name: os.environ.get(name) for name in FLAG_NAMES}

    def apply(*, write: bool, secret: bool, isolate: bool) -> None:
        for name, value in zip(FLAG_NAMES, (write, secret, isolate), strict=True):
            monkeypatch.setenv(name, "true" if value else "false")
        reload_config()

    yield apply

    for name, value in previous.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reload_config()


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", sorted(EXPECTED_TOOL_COUNTS))
async def test_server_registers_the_published_tool_count(flags, feature_flags):
    write, secret, isolate = flags
    feature_flags(write=write, secret=secret, isolate=isolate)
    # Dummy tenant credentials; building the server never calls the network.
    server = await initialize_mcp_server("dummy-key", "1", "example.invalid")
    tools = await server.list_tools()
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), "duplicate tool registrations inflate the published total"
    expected = EXPECTED_TOOL_COUNTS[flags]
    assert len(tools) == expected, (
        f"flags (write={write}, secret={secret}, isolate={isolate}) registered {len(tools)} tools, "
        f"expected {expected}. {_update_sites(flags)}"
    )


@pytest.mark.parametrize("flags", sorted(EXPECTED_TOOL_COUNTS))
def test_readme_publishes_the_guarded_tool_count(flags):
    lineno, line = _readme_totals_line()
    match = re.search(README_TOTAL_PATTERNS[flags], line)
    assert match is not None, (
        f"README.md:{lineno} no longer states the total for (write, secret, isolate)={flags} "
        f"in the expected wording. {_update_sites(flags)}"
    )
    documented = int(match.group(1))
    expected = EXPECTED_TOOL_COUNTS[flags]
    assert documented == expected, (
        f"README.md:{lineno} publishes {documented} tools for (write, secret, isolate)={flags}, "
        f"but the guarded count is {expected}. {_update_sites(flags)}"
    )
