"""Portkey session adapter in front of the stateless Streamable HTTP handler.

Portkey calls ``session.initialize`` with empty or omitted params and then
sends the returned ``Mcp-Session-Id`` on later requests. The 2026-07-28
handler stays stateless and does not mint that header. This adapter owns the
id, defaults a missing ``protocolVersion``, and forwards ``tools/list`` and
``tools/call`` without an upstream session.
"""

import asyncio
import contextlib
import hmac
import json
import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp.types import INVALID_REQUEST
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SESSION_HEADER = "mcp-session-id"
_PROTOCOL_HEADER = "mcp-protocol-version"
_INIT_METHODS = frozenset({"initialize", "session.initialize"})
_DEFAULT_CLIENT_INFO = {"name": "mcp", "version": "0"}
_MISSING_SESSION = "Bad Request: Missing session ID"
_UNKNOWN_SESSION = "Not Found: Invalid or expired session ID"
_BODY_TOO_LARGE = "Request body too large"
# Same idle window and ceiling the SDK uses for a stateful session. This store
# is the adapter's, not the stateless handler's.
_DEFAULT_IDLE_TIMEOUT = 30 * 60
_DEFAULT_MAX_SESSIONS = 10_000


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
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


def _authorization_matches(scope: Scope, token: str) -> bool:
    presented = _header(scope, "authorization")
    if presented is None or not presented.lower().startswith("bearer "):
        return False
    offered = presented[7:]
    if len(offered) != len(token):
        return False
    return hmac.compare_digest(offered, token)


def _replace_headers(scope: Scope, updates: Mapping[str, str], drop: frozenset[str]) -> Scope:
    drop_names = {name.lower().encode("latin-1") for name in drop}
    updates_encoded = {name.lower().encode("latin-1"): value.encode("latin-1") for name, value in updates.items()}
    headers: list[tuple[bytes, bytes]] = []
    for key, value in scope.get("headers", []):
        if key.lower() in drop_names or key.lower() in updates_encoded:
            continue
        headers.append((key, value))
    headers.extend(updates_encoded.items())
    return {**scope, "headers": headers}


def _chunk(message: Message) -> bytes:
    body = message.get("body", b"")
    if isinstance(body, bytes):
        return body
    return b""


def _replay(payload: bytes, receive: Receive) -> Receive:
    cached: deque[Message] = deque([{"type": "http.request", "body": payload, "more_body": False}])

    async def replay() -> Message:
        if cached:
            return cached.popleft()
        return await receive()

    return replay


def _json_object(payload: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(body, dict):
        return body
    return None


def _prepare_initialize(scope: Scope, body: dict[str, Any]) -> tuple[Scope, bytes]:
    """Rewrite Portkey's handshake into one the stateless handler can answer.

    A ``2026-07-28`` protocol header would enter the modern handler, which
    does not implement ``initialize``. The rewritten header stays on a
    handshake revision so the upstream call remains stateless.
    """
    raw_params = body.get("params")
    params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
    header_version = _header(scope, _PROTOCOL_HEADER)
    protocol = params.get("protocolVersion")
    if not isinstance(protocol, str) or not protocol:
        if header_version in HANDSHAKE_PROTOCOL_VERSIONS:
            protocol = header_version
        else:
            protocol = LATEST_HANDSHAKE_VERSION
        params["protocolVersion"] = protocol
    if "capabilities" not in params:
        params["capabilities"] = {}
    if "clientInfo" not in params:
        params["clientInfo"] = dict(_DEFAULT_CLIENT_INFO)
    route_version = header_version if header_version in HANDSHAKE_PROTOCOL_VERSIONS else protocol
    if route_version not in HANDSHAKE_PROTOCOL_VERSIONS:
        route_version = LATEST_HANDSHAKE_VERSION
    rewritten = {**body, "method": "initialize", "params": params}
    payload = json.dumps(rewritten).encode()
    forwarded = _replace_headers(
        scope,
        {
            "content-length": str(len(payload)),
            _PROTOCOL_HEADER: route_version,
        },
        drop=frozenset({_SESSION_HEADER, "mcp-method", "mcp-name"}),
    )
    return forwarded, payload


def _response_messages(payload: bytes, headers: list[tuple[bytes, bytes]]) -> list[Any]:
    content_type = ""
    for key, value in headers:
        if key.lower() == b"content-type":
            content_type = value.decode("latin-1")
            break
    if "text/event-stream" in content_type:
        messages: list[Any] = []
        for line in payload.splitlines():
            if not line.startswith(b"data:"):
                continue
            data = line[len(b"data:") :].strip()
            if not data:
                continue
            try:
                messages.append(json.loads(data))
            except json.JSONDecodeError:
                continue
        return messages
    parsed = _json_object(payload)
    if parsed is None:
        return []
    return [parsed]


def _initialize_succeeded(status: int, headers: list[tuple[bytes, bytes]], payload: bytes) -> bool:
    if status != 200:
        return False
    for message in _response_messages(payload, headers):
        if isinstance(message, dict) and "result" in message and "error" not in message:
            return True
    return False


def _with_session(headers: list[tuple[bytes, bytes]], session_id: str) -> list[tuple[bytes, bytes]]:
    kept = [(key, value) for key, value in headers if key.lower() != _SESSION_HEADER.encode("latin-1")]
    kept.append((_SESSION_HEADER.encode("latin-1"), session_id.encode("ascii")))
    return kept


async def _read_body(receive: Receive) -> bytes | None:
    """Return the POST body, or None when it exceeds the SDK body cap."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"".join(chunks)
        chunk = _chunk(message)
        if size + len(chunk) > DEFAULT_MAX_REQUEST_BODY_SIZE:
            return None
        chunks.append(chunk)
        size += len(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _exchange(app: ASGIApp, scope: Scope, receive: Receive) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Run the upstream app and return its completed response.

    The body is captured when the upstream sends the terminal chunk. A
    disconnect watcher still blocked on the client is cancelled after that,
    so the handshake can be inspected before the client sees it.
    """
    start: Message | None = None
    chunks: list[bytes] = []
    finished = asyncio.Event()

    async def capture(message: Message) -> None:
        nonlocal start
        if message["type"] == "http.response.start":
            start = message
            return
        if message["type"] == "http.response.body":
            chunk = _chunk(message)
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                finished.set()

    async def run() -> None:
        await app(scope, receive, capture)

    task: asyncio.Task[None] = asyncio.create_task(run())
    task.add_done_callback(lambda _completed: finished.set())
    await finished.wait()
    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    else:
        await task
    if start is None:
        raise RuntimeError("upstream sent no response")
    headers = start.get("headers", [])
    if not isinstance(headers, list):
        headers = []
    return int(start["status"]), headers, b"".join(chunks)


async def _send_response(send: Send, status: int, headers: list[tuple[bytes, bytes]], payload: bytes) -> None:
    kept = [(key, value) for key, value in headers if key.lower() != b"content-length"]
    kept.append((b"content-length", str(len(payload)).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": kept})
    await send({"type": "http.response.body", "body": payload, "more_body": False})


def _session_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": message}},
        status_code=status_code,
    )


class PortkeySessionAdapter:
    """Store a Portkey ``Mcp-Session-Id`` and require it after initialize.

    Each id lives for ``idle_timeout`` seconds after its last successful use.
    A later request that presents a live id refreshes that deadline. Past
    ``max_sessions``, the oldest idle id is dropped so an active one stays.
    """

    def __init__(
        self,
        app: ASGIApp,
        path: str,
        auth_token: str = "",
        *,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not (math.isfinite(idle_timeout) and idle_timeout > 0):
            raise ValueError("idle_timeout must be a positive, finite number of seconds")
        if max_sessions <= 0:
            raise ValueError("max_sessions must be a positive number of sessions")
        self.app = app
        self.path = path
        self.auth_token = auth_token
        self._idle_timeout = idle_timeout
        self._max_sessions = max_sessions
        self._clock = time.monotonic if clock is None else clock
        # Insertion order is least-recently-used. A successful use moves the id to the end.
        self._sessions: dict[str, float] = {}

    def _on_mcp_path(self, scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        path = scope.get("path", "")
        if not isinstance(path, str):
            return False
        return path.rstrip("/") == self.path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._on_mcp_path(scope):
            await self.app(scope, receive, send)
            return
        if self.auth_token and not _authorization_matches(scope, self.auth_token):
            await self.app(scope, receive, send)
            return
        declared = _declared_content_length(scope)
        if declared is not None and declared > DEFAULT_MAX_REQUEST_BODY_SIZE:
            await self.app(scope, receive, send)
            return

        payload = await _read_body(receive)
        if payload is None:
            await Response(_BODY_TOO_LARGE, status_code=413)(scope, receive, send)
            return

        body = _json_object(payload)
        method = body.get("method") if body is not None else None
        if method in _INIT_METHODS and body is not None:
            await self._initialize(scope, body, receive, send)
            return

        session_id = _header(scope, _SESSION_HEADER)
        if session_id is None or not session_id:
            await _session_error(_MISSING_SESSION, 400)(scope, receive, send)
            return
        if not self._accept(session_id):
            await _session_error(_UNKNOWN_SESSION, 404)(scope, receive, send)
            return

        forwarded = _replace_headers(scope, {}, drop=frozenset({_SESSION_HEADER}))
        await self.app(forwarded, _replay(payload, receive), send)

    def _purge(self, now: float) -> None:
        expired = [
            session_id for session_id, last_used in self._sessions.items() if now - last_used >= self._idle_timeout
        ]
        for session_id in expired:
            del self._sessions[session_id]

    def _trim(self) -> None:
        overflow = len(self._sessions) - self._max_sessions
        if overflow <= 0:
            return
        for session_id in list(self._sessions)[:overflow]:
            del self._sessions[session_id]

    def _remember(self, session_id: str) -> None:
        now = self._clock()
        self._purge(now)
        self._sessions.pop(session_id, None)
        self._sessions[session_id] = now
        self._trim()

    def _accept(self, session_id: str) -> bool:
        """Refresh a live id. An unknown or idle id is not a successful use."""
        now = self._clock()
        self._purge(now)
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
        self._sessions[session_id] = now
        return True

    async def _initialize(self, scope: Scope, body: dict[str, Any], receive: Receive, send: Send) -> None:
        """Answer the Portkey handshake without sending it to strict FastMCP.

        Portkey may add server-side metadata (for example ``serverInfo``) to a
        client ``session.initialize`` request. A stateless FastMCP endpoint
        must not be asked to parse that gateway-shaped payload. The adapter
        owns the session and handshake response; later tools requests still
        pass through to the stateless application.
        """
        raw_params = body.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        requested_version = params.get("protocolVersion")
        protocol = requested_version if requested_version in HANDSHAKE_PROTOCOL_VERSIONS else LATEST_HANDSHAKE_VERSION
        session_id = uuid4().hex
        self._remember(session_id)
        response = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": protocol,
                    "serverInfo": {"name": "Cortex MCP Server", "version": "2.0.0"},
                    "capabilities": {"tools": {}},
                },
            }
        ).encode()
        headers = _with_session([(b"content-type", b"application/json")], session_id)
        await _send_response(send, 200, headers, response)
