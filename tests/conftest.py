import os

import pytest

os.environ.setdefault("CORTEX_MCP_PAPI_URL", "https://api-test.xdr.us.paloaltonetworks.com")
os.environ.setdefault("CORTEX_MCP_PAPI_AUTH_HEADER", "test-secret")
os.environ.setdefault("CORTEX_MCP_PAPI_AUTH_ID", "1")
os.environ.setdefault("FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER", "true")

from config.config import reload_config  # noqa: E402
from main import initialize_mcp_server  # noqa: E402


@pytest.fixture
def flags(monkeypatch):
    """
    Set one or more MCP_*_ENABLED env vars for the duration of a test and reload config.
    Usage: flags(MCP_WRITE_TOOLS_ENABLED="true")
    """

    def _set(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return reload_config()

    yield _set
    reload_config()


@pytest.fixture
async def mcp_server():
    """A fully wired FastMCP server instance (all builtin + custom tools registered)."""
    return await initialize_mcp_server("test-secret", "1", "https://api-test.xdr.us.paloaltonetworks.com")
