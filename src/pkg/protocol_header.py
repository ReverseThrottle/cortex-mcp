"""Reject a 2026-07-28 body when the protocol header is not that revision.

The SDK routes a missing header, or a handshake revision, to the legacy
Streamable HTTP path and does not compare the header with the body. A body
that carries the modern protocol version must take the header check instead.
"""

import asyncio
import json
from typing import Any

from mcp.types import (
    HEADER_MISMATCH,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
)
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_PROTOCOL_HEADER = "mcp-protocol-version"


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
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


class ModernProtocolHeaderMiddleware:
    """Apply the modern header check before the SDK's handshake-era route."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        payload = b"".join(chunks)
        delivered = False
        finished = asyncio.Event()

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await finished.wait()
            return {"type": "http.disconnect"}

        rejection = modern_header_mismatch(scope, payload)
        try:
            if rejection is not None:
                response = JSONResponse(rejection, status_code=400)
                await response(scope, replay, send)
                return
            await self.app(scope, replay, send)
        finally:
            finished.set()
