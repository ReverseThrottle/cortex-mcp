"""Body-cap and disconnect behavior of the modern protocol header check."""

import asyncio
import json

import pytest
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp.types import HEADER_MISMATCH, LATEST_PROTOCOL_VERSION
from starlette.types import Message, Receive, Scope, Send

from pkg.protocol_header import ModernProtocolHeaderMiddleware

_MODERN_BODY = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/list",
    "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION}},
}


def _scope(content_length: int | None = None, protocol: str | None = None) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    if protocol is not None:
        headers.append((b"mcp-protocol-version", protocol.encode("ascii")))
    return {"type": "http", "method": "POST", "headers": headers}


def _sender(sent: list[Message]) -> Send:
    async def send(message: Message) -> None:
        sent.append(message)

    return send


@pytest.mark.asyncio
async def test_declared_oversize_is_not_read():
    async def receive() -> Message:
        raise AssertionError("declared oversize body was read")

    entered = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal entered
        entered = True
        await send({"type": "http.response.start", "status": 413, "headers": []})
        await send({"type": "http.response.body", "body": b"Request body too large"})

    sent: list[Message] = []
    await ModernProtocolHeaderMiddleware(app)(
        _scope(content_length=DEFAULT_MAX_REQUEST_BODY_SIZE + 1),
        receive,
        _sender(sent),
    )
    assert entered
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_body_stops_at_the_cap():
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "type": "http.request",
                "body": b"x" * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1),
                "more_body": True,
            }
        raise AssertionError("body read continued past the cap")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("oversized body reached the app")

    sent: list[Message] = []
    await ModernProtocolHeaderMiddleware(app)(_scope(), receive, _sender(sent))
    assert calls == 1
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b"Request body too large"


@pytest.mark.asyncio
async def test_handshake_header_on_a_modern_body_is_still_rejected():
    raw = json.dumps(_MODERN_BODY).encode()
    midpoint = len(raw) // 2
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": raw[:midpoint], "more_body": True}
        if calls == 2:
            return {"type": "http.request", "body": raw[midpoint:], "more_body": False}
        raise AssertionError("body read continued after the request ended")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("rejected body reached the app")

    sent: list[Message] = []
    await ModernProtocolHeaderMiddleware(app)(
        _scope(protocol="2025-06-18"),
        receive,
        _sender(sent),
    )
    assert sent[0]["status"] == 400
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == HEADER_MISMATCH
    assert body["id"] == 7
    assert "result" not in body


@pytest.mark.asyncio
async def test_client_disconnect_reaches_the_handler_before_it_returns():
    delivered = asyncio.Event()
    release = asyncio.Event()
    seen: list[Message] = []
    sent_body = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())
        delivered.set()
        seen.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def client_receive() -> Message:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await release.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(ModernProtocolHeaderMiddleware(app)(_scope(), client_receive, _sender([])))
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert seen[0]["type"] == "http.request"
    assert seen[0]["body"] == b"{}"
    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert seen[1] == {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_disconnect_waits_until_the_client_sends_it():
    """A second receive is not a synthetic disconnect.

    httpx's ASGI transport yields ``http.disconnect`` only after the response
    is complete. Returning one immediately aborts the response.
    """
    response_complete = asyncio.Event()
    phase = 0
    order: list[str] = []

    async def client_receive() -> Message:
        nonlocal phase
        phase += 1
        if phase == 1:
            return {"type": "http.request", "body": b"{}", "more_body": True}
        if phase == 2:
            return {"type": "http.request", "body": b"", "more_body": False}
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            order.append("response")
            response_complete.set()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        first = await receive()
        assert first["body"] == b"{}"
        assert first["more_body"] is False
        watcher = asyncio.create_task(receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        second = await watcher
        order.append(str(second["type"]))

    await asyncio.wait_for(
        ModernProtocolHeaderMiddleware(app)(_scope(), client_receive, send),
        timeout=1,
    )
    assert order == ["response", "http.disconnect"]
