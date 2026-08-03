"""
Smoke-tests the streamable-http transport end-to-end (server boot, /ping/, tools/list,
tool call) using a mocked PAPI backend — no tenant credentials required. This is what
proves the HTTP/streaming wiring itself is sound; live-tenant behavior is validated
separately once credentials are available (see the plan's verification checklist).
"""

import asyncio
import contextlib
import socket

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from main import initialize_mcp_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def streamable_http_server():
    port = _free_port()
    host = "127.0.0.1"
    path = "/api/v1/stream/mcp"

    mcp = await initialize_mcp_server("test-secret", "1", "https://api-test.xdr.us.paloaltonetworks.com")

    server_task = asyncio.create_task(
        mcp.run_async(transport="streamable-http", host=host, port=port, path=path, show_banner=False)
    )

    base_url = f"http://{host}:{port}"
    async with httpx.AsyncClient(base_url=base_url, timeout=5) as probe:
        for _ in range(50):
            if server_task.done():
                raise RuntimeError(f"Server task exited early: {server_task.exception()}")
            try:
                resp = await probe.get("/ping/")
                if resp.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("streamable-http server did not become ready in time")

    yield base_url, path

    server_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await server_task


async def test_ping_endpoint_responds(streamable_http_server):
    base_url, _ = streamable_http_server
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/ping/")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


async def test_tools_list_over_streamable_http(streamable_http_server):
    base_url, path = streamable_http_server
    async with Client(StreamableHttpTransport(f"{base_url}{path}")) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "get_tenant_info" in tool_names
        assert "get_cases" in tool_names


async def test_tool_call_over_streamable_http(streamable_http_server):
    """A read tool call should round-trip over the real HTTP/streaming transport all the way
    to the (unreachable-by-design) PAPI call — proving request/response framing works, not
    just connection setup. OpenAPI-sourced tools raise ToolError on a failed upstream request
    (unlike the custom Python tools, which catch and return an error JSON payload) — a DNS
    failure surfacing here as a ToolError is exactly the proof the request made the full round
    trip over streamable-http."""
    base_url, path = streamable_http_server
    async with Client(StreamableHttpTransport(f"{base_url}{path}")) as client:
        with pytest.raises(Exception, match="Name or service not known"):
            await client.call_tool("get_tenant_info", {})
