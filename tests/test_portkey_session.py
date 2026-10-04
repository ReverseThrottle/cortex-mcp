"""Portkey session.initialize in front of the stateless Streamable HTTP handler."""

import asyncio
import inspect
import json
from pathlib import Path

import httpx2
import pytest
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from starlette.middleware import Middleware
from starlette.types import Message, Receive, Scope, Send

from config.config import DEFAULT_MCP_ALLOWED_HOSTS
from main import async_main, streamable_http_middleware
from pkg.portkey_session import PortkeySessionAdapter
from pkg.protocol_header import ModernProtocolHeaderMiddleware
from service.cortex_mcp.server import create_mcp_server
from version import __version__

_ACCEPT = "application/json, text/event-stream"
_MODERN = "2026-07-28"
_HANDSHAKE = "2025-11-25"
_BEARER = "dummy-bearer"


def _messages(response: httpx2.Response) -> list[dict]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        found = []
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data:
                found.append(json.loads(data))
        return found
    if not response.content:
        return []
    body = response.json()
    if isinstance(body, dict):
        return [body]
    return []


def _result(response: httpx2.Response) -> dict | None:
    for message in _messages(response):
        result = message.get("result")
        if isinstance(result, dict):
            return result
    return None


def _session_id(response: httpx2.Response) -> str | None:
    return response.headers.get("mcp-session-id")


def _headers(**extra: str) -> dict[str, str]:
    headers = {"accept": _ACCEPT, "content-type": "application/json"}
    headers.update(extra)
    return headers


def _modern_meta() -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": _MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "conformance", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


class _Upstream:
    """Records the request the adapter forwards. Does not mint a session id."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body = message.get("body", b"")
            if isinstance(body, bytes):
                chunks.append(body)
            if not message.get("more_body", False):
                break
        payload = json.loads(b"".join(chunks))
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]}
        self.calls.append({"body": payload, "headers": headers})
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "serverInfo": {"name": "upstream", "version": "0"},
            }
            encoded = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode()
        elif payload["method"] == "tools/list":
            encoded = json.dumps(
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"tools": [{"name": "ping"}]}}
            ).encode()
        elif payload["method"] == "tools/call":
            encoded = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [{"type": "text", "text": "pong"}], "isError": False},
                }
            ).encode()
        else:
            encoded = json.dumps(
                {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32601, "message": "Method not found"}}
            ).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": encoded, "more_body": False})


def _adapter(upstream: _Upstream, auth_token: str = "") -> PortkeySessionAdapter:
    return PortkeySessionAdapter(upstream, path="/mcp", auth_token=auth_token)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _session_adapter(
    upstream: _Upstream,
    clock: _Clock,
    *,
    idle_timeout: float,
    max_sessions: int,
) -> PortkeySessionAdapter:
    return PortkeySessionAdapter(
        upstream,
        path="/mcp",
        idle_timeout=idle_timeout,
        max_sessions=max_sessions,
        clock=clock,
    )


async def _open_session(adapter: PortkeySessionAdapter, request_id: int) -> str:
    opened = await _post(
        adapter,
        {"jsonrpc": "2.0", "id": request_id, "method": "session.initialize", "params": {}},
        _headers(),
    )
    session_id = _session_id(opened)
    assert opened.status_code == 200
    assert session_id
    return session_id


async def _list_with(adapter: PortkeySessionAdapter, session_id: str, request_id: int) -> httpx2.Response:
    return await _post(
        adapter,
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}},
        _headers(**{"mcp-session-id": session_id}),
    )


async def _post(app, body: dict, headers: dict[str, str]) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.post("/mcp", headers=headers, json=body)


def _server_app(auth_token: str = ""):
    server = create_mcp_server("test-key", "1", auth_token or None)

    async def ping() -> str:
        """Side effects: none. Reply pong."""
        return "pong"

    server.add_tool(ping)
    return server.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=DEFAULT_MCP_ALLOWED_HOSTS.split(","),
        allowed_origins=[],
        middleware=streamable_http_middleware("/mcp", auth_token),
    )


def _bare_app():
    """The stateless handler with the protocol-header check and no session adapter."""
    server = create_mcp_server("test-key", "1")

    async def ping() -> str:
        return "pong"

    server.add_tool(ping)
    return server.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=DEFAULT_MCP_ALLOWED_HOSTS.split(","),
        allowed_origins=[],
        middleware=[Middleware(ModernProtocolHeaderMiddleware)],
    )


def test_http_startup_keeps_the_handler_stateless():
    text = Path(inspect.getsourcefile(async_main)).read_text()
    assert "stateless_http=True" in text
    assert "streamable_http_middleware" in text


@pytest.mark.asyncio
async def test_adapter_owns_the_session_and_forwards_without_one():
    upstream = _Upstream()
    adapter = _adapter(upstream, _BEARER)
    opened = await _post(
        adapter,
        {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
        _headers(
            **{
                "authorization": f"Bearer {_BEARER}",
                "mcp-protocol-version": _MODERN,
                "mcp-method": "session.initialize",
            }
        ),
    )
    session_id = _session_id(opened)
    assert opened.status_code == 200
    assert session_id
    assert _result(opened) is not None
    assert _result(opened)["serverInfo"]["name"] == "Cortex MCP Server"
    assert upstream.calls == []

    listed = await _post(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"_meta": _modern_meta()},
        },
        _headers(
            **{
                "authorization": f"Bearer {_BEARER}",
                "mcp-protocol-version": _MODERN,
                "mcp-method": "tools/list",
                "mcp-session-id": session_id,
            }
        ),
    )
    assert listed.status_code == 200
    assert _result(listed)["tools"][0]["name"] == "ping"
    assert _session_id(listed) is None
    tool_call = upstream.calls[0]
    assert tool_call["body"]["method"] == "tools/list"
    assert "mcp-session-id" not in tool_call["headers"]
    assert tool_call["headers"]["authorization"] == f"Bearer {_BEARER}"
    assert tool_call["headers"]["mcp-protocol-version"] == _MODERN

    called = await _post(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
        _headers(
            **{
                "authorization": f"Bearer {_BEARER}",
                "mcp-session-id": session_id,
            }
        ),
    )
    assert called.status_code == 200
    assert _result(called)["isError"] is False
    assert _result(called)["content"][0]["text"] == "pong"
    assert "mcp-session-id" not in upstream.calls[1]["headers"]


@pytest.mark.asyncio
async def test_gateway_added_metadata_is_not_forwarded_to_a_strict_upstream():
    upstream = _Upstream()
    adapter = _adapter(upstream)
    opened = await _post(
        adapter,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.initialize",
            "params": {
                "protocolVersion": _HANDSHAKE,
                "capabilities": {},
                "serverInfo": {"name": "gateway", "version": "1"},
            },
        },
        _headers(),
    )
    assert opened.status_code == 200
    assert _session_id(opened)
    result = _result(opened)
    assert result is not None
    assert result["protocolVersion"] == _HANDSHAKE
    assert result["serverInfo"]["name"] == "Cortex MCP Server"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_stateless_handler_does_not_mint_a_session_id():
    app = _bare_app()
    async with app.router.lifespan_context(app):
        opened = await _post(
            app,
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            _headers(),
        )
        listed = await _post(
            app,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": _modern_meta()},
            },
            _headers(**{"mcp-protocol-version": _MODERN, "mcp-method": "tools/list"}),
        )
    assert opened.status_code == 200
    assert _session_id(opened) is None
    assert _result(opened) is None
    assert listed.status_code == 200
    assert _session_id(listed) is None
    assert _result(listed) is not None


@pytest.mark.asyncio
async def test_portkey_session_initialize_then_tools_list_and_call():
    app = _server_app()
    probes = [
        (_headers(), {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}}),
        (
            _headers(**{"mcp-protocol-version": _HANDSHAKE}),
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
        ),
        (
            _headers(**{"mcp-protocol-version": _MODERN, "mcp-method": "session.initialize"}),
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
        ),
        (_headers(), {"jsonrpc": "2.0", "id": 1, "method": "session.initialize"}),
    ]
    async with app.router.lifespan_context(app):
        for headers, body in probes:
            opened = await _post(app, body, headers)
            session_id = _session_id(opened)
            result = _result(opened)
            assert opened.status_code == 200
            assert session_id
            assert result is not None
            assert result["protocolVersion"] == _HANDSHAKE
            assert result["serverInfo"]["version"] == __version__
            assert "error" not in _messages(opened)[-1]

            modern_list = await _post(
                app,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {"_meta": _modern_meta()},
                },
                _headers(
                    **{
                        "mcp-protocol-version": _MODERN,
                        "mcp-method": "tools/list",
                        "mcp-session-id": session_id,
                    }
                ),
            )
            assert modern_list.status_code == 200
            assert _session_id(modern_list) is None
            names = [tool["name"] for tool in _result(modern_list)["tools"]]
            assert "ping" in names

            modern_call = await _post(
                app,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "ping",
                        "arguments": {},
                        "_meta": _modern_meta(),
                    },
                },
                _headers(
                    **{
                        "mcp-protocol-version": _MODERN,
                        "mcp-method": "tools/call",
                        "mcp-name": "ping",
                        "mcp-session-id": session_id,
                    }
                ),
            )
            assert modern_call.status_code == 200
            assert _result(modern_call)["isError"] is False
            assert _result(modern_call)["content"][0]["text"] == "pong"

            handshake_list = await _post(
                app,
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                _headers(**{"mcp-protocol-version": _HANDSHAKE, "mcp-session-id": session_id}),
            )
            assert handshake_list.status_code == 200
            handshake_names = [tool["name"] for tool in _result(handshake_list)["tools"]]
            assert "ping" in handshake_names

            handshake_call = await _post(
                app,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "ping", "arguments": {}},
                },
                _headers(**{"mcp-protocol-version": _HANDSHAKE, "mcp-session-id": session_id}),
            )
            assert handshake_call.status_code == 200
            assert _result(handshake_call)["content"][0]["text"] == "pong"


@pytest.mark.asyncio
async def test_standard_initialize_mints_a_session_including_empty_params():
    app = _server_app()
    async with app.router.lifespan_context(app):
        full = await _post(
            app,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _HANDSHAKE,
                    "capabilities": {},
                    "clientInfo": {"name": "conformance", "version": "0"},
                },
            },
            _headers(**{"mcp-protocol-version": _HANDSHAKE}),
        )
        empty = await _post(
            app,
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
            _headers(),
        )
    assert _session_id(full)
    assert _result(full)["protocolVersion"] == _HANDSHAKE
    assert _result(full)["serverInfo"]["version"] == __version__
    assert _session_id(empty)
    assert _session_id(empty) != _session_id(full)
    assert _result(empty)["protocolVersion"] == _HANDSHAKE
    assert "error" not in _messages(empty)[-1]


@pytest.mark.asyncio
async def test_later_requests_require_the_stored_session_id():
    app = _server_app()
    listed = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {"_meta": _modern_meta()},
    }
    async with app.router.lifespan_context(app):
        opened = await _post(
            app,
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            _headers(),
        )
        session_id = _session_id(opened)
        missing = await _post(
            app,
            listed,
            _headers(**{"mcp-protocol-version": _MODERN, "mcp-method": "tools/list"}),
        )
        invented = await _post(
            app,
            listed,
            _headers(
                **{
                    "mcp-protocol-version": _MODERN,
                    "mcp-method": "tools/list",
                    "mcp-session-id": "portkey-session-1",
                }
            ),
        )
        handshake_missing = await _post(
            app,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            _headers(**{"mcp-protocol-version": _HANDSHAKE}),
        )
        accepted = await _post(
            app,
            listed,
            _headers(
                **{
                    "mcp-protocol-version": _MODERN,
                    "mcp-method": "tools/list",
                    "mcp-session-id": session_id,
                }
            ),
        )
    assert missing.status_code == 400
    assert _session_id(missing) is None
    assert missing.json()["error"]["code"] == -32600
    assert "session ID" in missing.json()["error"]["message"]
    assert invented.status_code == 404
    assert _session_id(invented) is None
    assert "session ID" in invented.json()["error"]["message"]
    assert handshake_missing.status_code == 400
    assert accepted.status_code == 200
    assert "ping" in [tool["name"] for tool in _result(accepted)["tools"]]


@pytest.mark.asyncio
async def test_missing_bearer_stays_unauthorized_and_mints_no_session():
    app = _server_app(_BEARER)
    async with app.router.lifespan_context(app):
        opened = await _post(
            app,
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            _headers(),
        )
        listed = await _post(
            app,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            _headers(**{"mcp-protocol-version": _HANDSHAKE, "mcp-session-id": "portkey-session-1"}),
        )
        authed = await _post(
            app,
            {"jsonrpc": "2.0", "id": 3, "method": "session.initialize", "params": {}},
            _headers(**{"authorization": f"Bearer {_BEARER}"}),
        )
        session_id = _session_id(authed)
        without_bearer = await _post(
            app,
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            _headers(**{"mcp-protocol-version": _HANDSHAKE, "mcp-session-id": session_id or ""}),
        )
        with_bearer = await _post(
            app,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
            _headers(
                **{
                    "authorization": f"Bearer {_BEARER}",
                    "mcp-protocol-version": _HANDSHAKE,
                    "mcp-session-id": session_id or "",
                }
            ),
        )
    assert opened.status_code == 401
    assert _session_id(opened) is None
    assert listed.status_code == 401
    assert _session_id(listed) is None
    assert authed.status_code == 200
    assert session_id
    assert without_bearer.status_code == 401
    assert _session_id(without_bearer) is None
    assert with_bearer.status_code == 200
    assert "ping" in [tool["name"] for tool in _result(with_bearer)["tools"]]


@pytest.mark.asyncio
async def test_rejected_host_does_not_open_a_session():
    app = _server_app()
    async with app.router.lifespan_context(app):
        rejected = await _post(
            app,
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            _headers(**{"host": "evil.example", "origin": "http://evil.example"}),
        )
    assert rejected.status_code == 421
    assert _session_id(rejected) is None


@pytest.mark.asyncio
async def test_declared_oversize_post_is_not_read_by_the_adapter():
    app = _server_app()
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        raise AssertionError("oversized request body was read")

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
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
            (b"accept", _ACCEPT.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(DEFAULT_MAX_REQUEST_BODY_SIZE + 1).encode("ascii")),
            (b"mcp-protocol-version", _MODERN.encode()),
            (b"mcp-method", b"tools/call"),
            (b"mcp-name", b"ping"),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("127.0.0.1", 80),
        "root_path": "",
    }
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)
    assert reads == 0
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_client_disconnect_still_cancels_an_in_flight_tool_call():
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
        middleware=streamable_http_middleware("/mcp"),
    )
    async with app.router.lifespan_context(app):
        opened = await _post(
            app,
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            _headers(),
        )
        session_id = _session_id(opened)
        assert session_id
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "hold_open", "arguments": {}},
            }
        ).encode()
        sent_body = False

        async def receive() -> Message:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            return None

        scope = {
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
                (b"accept", _ACCEPT.encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"mcp-protocol-version", _HANDSHAKE.encode()),
                (b"mcp-session-id", session_id.encode("ascii")),
            ],
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
            "root_path": "",
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=2)

    assert started.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_idle_session_expires_and_a_used_session_stays_valid():
    clock = _Clock()
    upstream = _Upstream()
    adapter = _session_adapter(upstream, clock, idle_timeout=30, max_sessions=10)
    abandoned = await _open_session(adapter, 1)

    clock.now = 30
    expired = await _list_with(adapter, abandoned, 2)
    assert expired.status_code == 404
    assert expired.json()["error"]["code"] == -32600
    assert "session ID" in expired.json()["error"]["message"]
    assert _session_id(expired) is None
    assert len(upstream.calls) == 0

    clock.now = 100
    active = await _open_session(adapter, 3)
    clock.now = 129
    refreshed = await _list_with(adapter, active, 4)
    assert refreshed.status_code == 200
    assert _result(refreshed)["tools"][0]["name"] == "ping"
    assert "mcp-session-id" not in upstream.calls[-1]["headers"]

    clock.now = 158
    still_valid = await _list_with(adapter, active, 5)
    assert still_valid.status_code == 200

    clock.now = 188
    idle_again = await _list_with(adapter, active, 6)
    assert idle_again.status_code == 404
    assert idle_again.json()["error"]["code"] == -32600
    assert len(upstream.calls) == 2


@pytest.mark.asyncio
async def test_session_cap_evicts_the_oldest_idle_id():
    clock = _Clock()
    upstream = _Upstream()
    adapter = _session_adapter(upstream, clock, idle_timeout=1000, max_sessions=2)
    created = []
    for request_id in range(3):
        clock.now = float(request_id)
        created.append(await _open_session(adapter, request_id))

    evicted = await _list_with(adapter, created[0], 10)
    assert evicted.status_code == 404
    assert evicted.json()["error"]["code"] == -32600
    assert len(upstream.calls) == 0

    clock.now = 10
    kept = await _list_with(adapter, created[1], 11)
    assert kept.status_code == 200

    clock.now = 11
    newest = await _open_session(adapter, 12)
    oldest_idle = await _list_with(adapter, created[2], 13)
    assert oldest_idle.status_code == 404
    assert (await _list_with(adapter, created[1], 14)).status_code == 200
    assert (await _list_with(adapter, newest, 15)).status_code == 200
    assert "mcp-session-id" not in upstream.calls[-1]["headers"]


@pytest.mark.asyncio
async def test_delete_stays_method_not_allowed_and_keeps_the_session():
    app = _server_app()
    async with app.router.lifespan_context(app):
        opened = await _post(
            app,
            {"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            _headers(),
        )
        session_id = _session_id(opened)
        assert session_id
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            deleted = await client.delete("/mcp", headers={"host": "127.0.0.1"})
        listed = await _post(
            app,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            _headers(**{"mcp-protocol-version": _HANDSHAKE, "mcp-session-id": session_id}),
        )
    assert deleted.status_code == 405
    assert listed.status_code == 200
    assert "ping" in [tool["name"] for tool in _result(listed)["tools"]]
