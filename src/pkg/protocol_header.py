"""Reject a 2026-07-28 body when the protocol header is not that revision.

The SDK routes a missing header, or a handshake revision, to the legacy
Streamable HTTP path and does not compare the header with the body. A body
that carries the modern protocol version must take the header check instead.

Reading stops at the SDK's request-body cap, and a larger declared
Content-Length is left for that cap to reject. After the buffered body is
replayed, further receives come from the client, so a disconnect is visible
while the handler is still running.
"""

import json
from collections import deque
from typing import Any

from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp.types import (
    HEADER_MISMATCH,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
)
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PROTOCOL_HEADER = "mcp-protocol-version"
_BODY_TOO_LARGE = "Request body too large"


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
    return None


def _declared_content_length(scope: Scope) -> int | None:
    raw = _header(scope, "content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _request_id(body: dict[str, Any]) -> str | int | None:
    request_id = body.get("id")
    if isinstance(request_id, str | int):
        return request_id
    return None


def modern_header_mismatch(scope: Scope, payload: bytes) -> dict[str, Any] | None:
    """Return a JSON-RPC error when the body revision and the header disagree."""
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    protocol_version = meta.get(PROTOCOL_VERSION_META_KEY)
    if protocol_version != LATEST_PROTOCOL_VERSION:
        return None
    if _header(scope, _PROTOCOL_HEADER) == protocol_version:
        return None
    return {
        "jsonrpc": "2.0",
        "id": _request_id(body),
        "error": {
            "code": HEADER_MISMATCH,
            "message": (f"{_PROTOCOL_HEADER} header does not match the request envelope's protocol version"),
        },
    }


def _chunk(message: Message) -> bytes:
    body = message.get("body", b"")
    if isinstance(body, bytes):
        return body
    return b""


def _replay(cached: deque[Message], receive: Receive) -> Receive:
    async def replay() -> Message:
        if cached:
            return cached.popleft()
        return await receive()

    return replay


class ModernProtocolHeaderMiddleware:
    """Apply the modern header check before the SDK's handshake-era route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > DEFAULT_MAX_REQUEST_BODY_SIZE:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        saw_request = False
        body_complete = False
        trailing: Message | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                trailing = message
                break
            chunk = _chunk(message)
            if size + len(chunk) > DEFAULT_MAX_REQUEST_BODY_SIZE:
                await Response(_BODY_TOO_LARGE, status_code=413)(scope, receive, send)
                return
            saw_request = True
            chunks.append(chunk)
            size += len(chunk)
            if not message.get("more_body", False):
                body_complete = True
                break

        payload = b"".join(chunks)
        cached: deque[Message] = deque()
        if saw_request:
            cached.append({"type": "http.request", "body": payload, "more_body": not body_complete})
        if trailing is not None:
            cached.append(trailing)
        replay = _replay(cached, receive)
        rejection = modern_header_mismatch(scope, payload)
        if rejection is not None:
            await JSONResponse(rejection, status_code=400)(scope, replay, send)
            return
        await self.app(scope, replay, send)
