"""Protocol checks that do not call a Cortex tenant."""

import asyncio
import json

import httpx2
import pytest
from fastmcp import Client, FastMCP
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp.types import HEADER_MISMATCH, LATEST_PROTOCOL_VERSION
from starlette.middleware import Middleware

from config.config import DEFAULT_MCP_ALLOWED_HOSTS, Settings
from main import resolve_transport
from pkg.input_validation import JsonSchemaInputMiddleware
from pkg.protocol_header import ModernProtocolHeaderMiddleware
from pkg.response_envelope import ResponseEnvelopeMiddleware
from pkg.tool_rate_limit import ToolCallRateLimitMiddleware
from pkg.util import create_response
from service.cortex_mcp.server import create_mcp_server
from usecase.base_module import BaseModule
from version import __version__


def test_package_speaks_the_july_2026_revision():
    assert LATEST_PROTOCOL_VERSION == "2026-07-28"
    assert __version__ == "2.0.0"
    assert Settings.model_fields["mcp_host"].default == "127.0.0.1"
    assert Settings.model_fields["mcp_allowed_hosts"].default == DEFAULT_MCP_ALLOWED_HOSTS
    assert DEFAULT_MCP_ALLOWED_HOSTS == "127.0.0.1,localhost,::1"


def test_sse_transport_is_rejected():
    assert resolve_transport("stdio") == "stdio"
    assert resolve_transport("streamable-http") == "streamable-http"
    assert resolve_transport("http") == "streamable-http"
    with pytest.raises(ValueError, match="HTTP\\+SSE"):
        resolve_transport("sse")
    with pytest.raises(ValueError, match="not supported"):
        resolve_transport("websocket")


class _Echo(BaseModule):
    def register_tools(self):
        self._add_tool(self.echo)
        self._add_tool(self.needs_int)

    def register_resources(self):
        return None

    async def echo(self) -> str:
        """Side effects: none. Echo a successful envelope."""
        return create_response({"reply": "ok"})

    async def needs_int(self, value: int) -> str:
        """Side effects: none. Require an integer argument."""
        return create_response({"reply": value})


def _server() -> FastMCP:
    server = create_mcp_server("test-key", "1")
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.add_middleware(JsonSchemaInputMiddleware())
    _Echo(server).register_tools()
    return server


@pytest.mark.asyncio
async def test_tools_list_and_call_include_the_2026_result_fields():
    server = _server()
    async with Client(server, mode="2026-07-28") as client:
        listed = await client.list_tools_mcp()
        called = await client.call_tool_mcp("echo", {})

    listed_wire = listed.model_dump(by_alias=True, exclude_none=True)
    assert listed_wire["resultType"] == "complete"
    assert listed_wire["ttlMs"] == 300000
    assert listed_wire["cacheScope"] == "private"

    called_wire = called.model_dump(by_alias=True, exclude_none=True)
    assert called_wire["resultType"] == "complete"
    assert called.is_error is False
    assert "ttlMs" not in called_wire
    assert server.version == "2.0.0"

    echo = next(tool for tool in listed.tools if tool.name == "echo")
    annotations = echo.annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.open_world_hint is True
    assert echo.title == "Echo a successful envelope"
    assert echo.output_schema == {"type": "object", "additionalProperties": True}


@pytest.mark.asyncio
async def test_json_schema_rejects_a_bad_argument_and_keeps_the_envelope():
    server = _server()
    async with Client(server, mode="2026-07-28") as client:
        refused = await client.call_tool_mcp("needs_int", {"value": "nope"})
        accepted = await client.call_tool_mcp("needs_int", {"value": 3})

    assert refused.is_error is True
    body = json.loads(refused.content[0].text)
    assert body["success"] == "false"
    assert "Invalid arguments" in body["error"]
    assert body["_metadata"]["formatting_instructions"]
    assert json.loads(accepted.content[0].text)["reply"] == 3


@pytest.mark.asyncio
async def test_tool_call_rate_limit_does_not_count_tools_list():
    server = FastMCP("limited")
    server.add_middleware(
        ToolCallRateLimitMiddleware(max_requests_per_second=0.001, burst_capacity=1, global_limit=True)
    )

    async def ping() -> str:
        return "ok"

    server.add_tool(ping)
    async with Client(server) as client:
        await client.list_tools()
        await client.call_tool_mcp("ping", {})
        await client.list_tools()
        limited = await client.call_tool_mcp("ping", {})

    assert limited.is_error is True
    body = json.loads(limited.content[0].text)
    assert body["success"] == "false"
    assert "rate limit" in body["error"].lower()
    assert body["_metadata"]["formatting_instructions"]
    assert limited.structured_content == body


@pytest.mark.asyncio
async def test_streamable_http_has_no_get_stream_and_checks_origin():
    server = create_mcp_server("test-key", "1")
    app = server.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=DEFAULT_MCP_ALLOWED_HOSTS.split(","),
        allowed_origins=[],
        middleware=[Middleware(ModernProtocolHeaderMiddleware)],
    )
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2026-07-28",
        "mcp-method": "tools/list",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "conformance", "version": "0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            missing_origin = await client.post("/mcp", headers=headers, json=body)
            forbidden = await client.post(
                "/mcp",
                headers={**headers, "origin": "https://evil.example"},
                json=body,
            )
            get_stream = await client.get("/mcp", headers={"accept": "text/event-stream"})
            rebound = await client.post(
                "/mcp",
                headers={**headers, "host": "evil.example", "origin": "http://evil.example"},
                json=body,
            )
            mixed = await client.post(
                "/mcp",
                headers={**headers, "mcp-protocol-version": "2025-06-18"},
                json=body,
            )

    assert missing_origin.status_code == 200
    assert "mcp-session-id" not in {key.lower() for key in missing_origin.headers}
    payload = missing_origin.json()
    assert payload["result"]["resultType"] == "complete"
    assert payload["result"]["ttlMs"] == 300000
    assert payload["result"]["cacheScope"] == "private"
    assert forbidden.status_code == 403
    assert get_stream.status_code == 405
    assert rebound.status_code == 421
    assert mixed.status_code == 400
    mixed_body = mixed.json()
    assert mixed_body["error"]["code"] == HEADER_MISMATCH
    assert "result" not in mixed_body


def _http_scope(content_length: int) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-type", b"application/json"),
            (b"content-length", str(content_length).encode("ascii")),
            (b"mcp-protocol-version", b"2026-07-28"),
            (b"mcp-method", b"tools/call"),
            (b"mcp-name", b"hold_open"),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("127.0.0.1", 80),
        "root_path": "",
    }


@pytest.mark.asyncio
async def test_declared_oversize_post_is_rejected_before_the_body_is_read():
    server = create_mcp_server("test-key", "1")
    app = server.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=DEFAULT_MCP_ALLOWED_HOSTS.split(","),
        allowed_origins=[],
        middleware=[Middleware(ModernProtocolHeaderMiddleware)],
    )
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        raise AssertionError("oversized request body was read")

    sent = []

    async def send(message):
        sent.append(message)

    async with app.router.lifespan_context(app):
        await app(_http_scope(DEFAULT_MAX_REQUEST_BODY_SIZE + 1), receive, send)

    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Request body too large"
    assert reads == 0


@pytest.mark.asyncio
async def test_client_disconnect_cancels_an_in_flight_tool_call():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hold_open() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "done"

    server = create_mcp_server("test-key", "1")
    server.add_tool(hold_open)
    app = server.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=DEFAULT_MCP_ALLOWED_HOSTS.split(","),
        allowed_origins=[],
        middleware=[Middleware(ModernProtocolHeaderMiddleware)],
    )
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "hold_open",
                "arguments": {},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "conformance", "version": "0"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
    ).encode()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app(_http_scope(len(body)), receive, send), timeout=2)

    assert started.is_set()
    assert cancelled.is_set()
