"""Behavioral startup/shutdown harness for async_main (issue #67).

Drives the real ``async_main`` and ``shutdown`` with only the process
boundaries stubbed: server construction (covered by
test_server_registration.py / test_public_api_catalog.py) and the FastMCP
runners that would bind a socket or stdio. Config resolution, signal
registration, transport dispatch, the HTTP kwargs, and task cancellation
all run for real and are asserted on observed behavior — not source text.
"""

import asyncio
import logging
import os
import signal

import httpx2
import pytest
from fastmcp import FastMCP

import main
from config.config import reload_config
from pkg.portkey_session import PortkeySessionAdapter
from pkg.protocol_header import ModernProtocolHeaderMiddleware

_ENV = {
    "CORTEX_MCP_PAPI_AUTH_HEADER": "dummy-key",
    "CORTEX_MCP_PAPI_AUTH_ID": "1",
    "CORTEX_MCP_PAPI_URL": "example.invalid",
    "MCP_AUTH_TOKEN": "dummy-bearer",
    "MCP_HOST": "127.0.0.1",
    "MCP_PORT": "8123",
    "MCP_PATH": "/mcp",
    # Whitespace around entries proves the csv_list resolution behaviorally.
    "MCP_ALLOWED_HOSTS": "127.0.0.1, localhost ,cortex.example",
    "MCP_ALLOWED_ORIGINS": "https://console.example, https://ops.example",
}


@pytest.fixture
def startup_env(monkeypatch):
    previous = {name: os.environ.get(name) for name in _ENV}
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    reload_config()
    yield _ENV
    for name, value in previous.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reload_config()


def _stub_runners(monkeypatch) -> dict[str, list]:
    """Record every FastMCP runner dispatch instead of serving."""
    calls: dict[str, list] = {"http": [], "stdio": []}

    async def fake_run_http_async(self, **kwargs):
        calls["http"].append((self, kwargs))

    async def fake_run_async(self, **kwargs):
        calls["stdio"].append((self, kwargs))

    monkeypatch.setattr(FastMCP, "run_http_async", fake_run_http_async)
    monkeypatch.setattr(FastMCP, "run_async", fake_run_async)
    return calls


def _stub_server_factory(monkeypatch) -> list[tuple]:
    """Return a real (empty) FastMCP; record the credentials async_main forwards."""
    created: list[tuple] = []

    async def fake_initialize(api_key, api_key_id, papi_url, auth_token=""):
        created.append((api_key, api_key_id, papi_url, auth_token))
        return FastMCP("lifecycle-test")

    monkeypatch.setattr(main, "initialize_mcp_server", fake_initialize)
    return created


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["streamable-http", "http"])
async def test_http_startup_dispatches_stateless_kwargs(startup_env, monkeypatch, transport):
    calls = _stub_runners(monkeypatch)
    created = _stub_server_factory(monkeypatch)

    await main.async_main(transport)

    assert calls["stdio"] == []
    assert len(calls["http"]) == 1
    server, kwargs = calls["http"][0]
    assert isinstance(server, FastMCP)
    assert created == [("dummy-key", "1", "example.invalid", "dummy-bearer")]
    assert kwargs["transport"] == "streamable-http"
    assert kwargs["host"] == startup_env["MCP_HOST"]
    assert kwargs["port"] == 8123
    assert kwargs["path"] == "/mcp"
    assert kwargs["stateless_http"] is True
    assert kwargs["host_origin_protection"] is True
    assert kwargs["allowed_hosts"] == ["127.0.0.1", "localhost", "cortex.example"]
    assert kwargs["allowed_origins"] == ["https://console.example", "https://ops.example"]
    middleware = kwargs["middleware"]
    expected = main.streamable_http_middleware("/mcp", "dummy-bearer")
    assert [(m.cls, m.kwargs) for m in middleware] == [(m.cls, m.kwargs) for m in expected]
    assert [m.cls for m in middleware] == [PortkeySessionAdapter, ModernProtocolHeaderMiddleware]
    assert middleware[0].kwargs == {"path": "/mcp", "auth_token": "dummy-bearer"}


@pytest.mark.asyncio
async def test_stdio_startup_dispatches_run_async(startup_env, monkeypatch):
    calls = _stub_runners(monkeypatch)
    created = _stub_server_factory(monkeypatch)

    await main.async_main("stdio")

    assert calls["http"] == []
    assert len(calls["stdio"]) == 1
    server, kwargs = calls["stdio"][0]
    assert isinstance(server, FastMCP)
    assert kwargs == {"transport": "stdio"}
    assert created == [("dummy-key", "1", "example.invalid", "dummy-bearer")]


@pytest.mark.asyncio
async def test_startup_registers_sigint_and_sigterm_handlers(startup_env, monkeypatch):
    _stub_runners(monkeypatch)
    _stub_server_factory(monkeypatch)
    loop = asyncio.get_running_loop()

    registered = {}
    real_add = loop.add_signal_handler

    def spy(sig, callback, *args):
        registered[sig] = (callback, args)
        real_add(sig, callback, *args)

    monkeypatch.setattr(loop, "add_signal_handler", spy)

    scheduled = []

    async def fake_shutdown(sig, passed_loop):
        scheduled.append((sig, passed_loop))

    monkeypatch.setattr(main, "shutdown", fake_shutdown)

    try:
        await main.async_main("stdio")
        # Handlers are really installed on the running loop, for both signals.
        assert set(registered) == {signal.SIGINT, signal.SIGTERM}
        for sig in (signal.SIGINT, signal.SIGTERM):
            callback, args = registered[sig]
            assert args == ()
            callback()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending, return_exceptions=True)
        # Each handler schedules shutdown with its own signal and this loop.
        assert scheduled == [(signal.SIGINT, loop), (signal.SIGTERM, loop)]
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


@pytest.mark.asyncio
async def test_shutdown_cancels_outstanding_tasks_and_stops_the_loop():
    stopped = []

    class _Loop:
        def stop(self):
            stopped.append(True)

    cleaned_up = asyncio.Event()

    async def well_behaved():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    async def raises_on_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("boom during cleanup") from None

    async def already_done():
        return 42

    tasks = [
        asyncio.create_task(well_behaved()),
        asyncio.create_task(raises_on_cancel()),
        asyncio.create_task(already_done()),
    ]
    await asyncio.sleep(0)  # let the outstanding tasks start

    # Runs inside the current task, which shutdown must exclude from cancellation.
    await main.shutdown(signal.SIGTERM, _Loop())

    assert all(t.done() for t in tasks)
    assert tasks[0].cancelled()
    assert cleaned_up.is_set()
    # gather(return_exceptions=True): a failing cleanup neither raises nor stalls.
    assert not tasks[1].cancelled()
    assert isinstance(tasks[1].exception(), RuntimeError)
    assert tasks[2].result() == 42
    assert stopped == [True]
    current = asyncio.current_task()
    assert current is not None and not current.cancelled()


class _RecordHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.mark.asyncio
async def test_http_without_auth_token_warns_but_stdio_does_not(startup_env, monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    reload_config()
    _stub_runners(monkeypatch)
    _stub_server_factory(monkeypatch)
    # setup_logging clears root handlers, so record on the named logger itself.
    handler = _RecordHandler()
    logger = logging.getLogger("Cortex MCP")
    logger.addHandler(handler)
    try:
        await main.async_main("streamable-http")
        assert any(
            r.levelno == logging.WARNING and "MCP_AUTH_TOKEN is not set" in r.getMessage() for r in handler.records
        )
        handler.records.clear()
        await main.async_main("stdio")
        assert not [r for r in handler.records if r.levelno >= logging.WARNING]
    finally:
        logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_startup_kwargs_keep_the_handler_stateless(startup_env, monkeypatch):
    """Behavioral replacement for the old source-text substring pin (issue #67).

    Builds the app from the kwargs async_main actually passed to the runner
    and asserts the protocol guarantee: PortkeySessionAdapter owns the
    session, the handler mints no Mcp-Session-Id, and no standalone GET SSE
    stream is offered.
    """
    calls = _stub_runners(monkeypatch)
    _stub_server_factory(monkeypatch)
    await main.async_main("streamable-http")
    server, kwargs = calls["http"][0]

    async def ping() -> str:
        """Side effects: none. Reply pong."""
        return "pong"

    server.add_tool(ping)
    app_kwargs = {k: v for k, v in kwargs.items() if k not in ("host", "port")}
    app = server.http_app(**app_kwargs)
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": "Bearer dummy-bearer",
    }
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            opened = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "session.initialize", "params": {}},
            )
            session_id = opened.headers.get("mcp-session-id")
            assert opened.status_code == 200
            assert session_id  # the adapter, not the handler, owns the session

            listed = await client.post(
                "/mcp",
                headers={**headers, "mcp-session-id": session_id, "mcp-protocol-version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200
            assert "ping" in listed.text
            assert "mcp-session-id" not in listed.headers  # handler mints none

            streamed = await client.get(
                "/mcp",
                headers={
                    "accept": "text/event-stream",
                    "authorization": "Bearer dummy-bearer",
                    "mcp-session-id": session_id,
                },
            )
            assert streamed.status_code == 405  # no standalone GET SSE stream


@pytest.mark.asyncio
async def test_require_mode_stops_http_startup_before_the_server_is_built(startup_env, monkeypatch):
    calls = _stub_runners(monkeypatch)
    created = _stub_server_factory(monkeypatch)
    monkeypatch.setenv("MCP_WRITE_CONFIRMATION", "require")
    reload_config()
    try:
        with pytest.raises(ValueError, match="MCP_WRITE_CONFIRMATION=require"):
            await main.async_main("streamable-http")
    finally:
        monkeypatch.delenv("MCP_WRITE_CONFIRMATION", raising=False)
        reload_config()

    assert created == []
    assert calls["http"] == []


@pytest.mark.asyncio
async def test_unknown_tool_name_stops_startup_before_the_server_is_built(startup_env, monkeypatch):
    calls = _stub_runners(monkeypatch)
    created = _stub_server_factory(monkeypatch)
    monkeypatch.setenv("MCP_TOOLS_DENY", "not_a_tool")
    reload_config()
    try:
        with pytest.raises(ValueError, match="not_a_tool"):
            await main.async_main("stdio")
    finally:
        monkeypatch.delenv("MCP_TOOLS_DENY", raising=False)
        reload_config()

    assert created == []
    assert calls["stdio"] == []


@pytest.mark.asyncio
async def test_transport_is_resolved_once_per_start(startup_env, monkeypatch):
    _stub_runners(monkeypatch)
    _stub_server_factory(monkeypatch)
    seen: list[str] = []
    real = main.resolve_transport

    def counting(transport: str) -> str:
        seen.append(transport)
        return real(transport)

    monkeypatch.setattr(main, "resolve_transport", counting)
    await main.async_main("http")
    assert seen == ["http"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["streamable-http", "http"])
async def test_deprecated_elicitation_variable_stops_http_startup_before_the_server_is_built(
    startup_env, monkeypatch, transport
):
    calls = _stub_runners(monkeypatch)
    created = _stub_server_factory(monkeypatch)
    monkeypatch.delenv("MCP_WRITE_CONFIRMATION", raising=False)
    monkeypatch.setenv("MCP_ELICITATION_ENABLED", "true")
    reload_config()
    try:
        with pytest.raises(ValueError, match="MCP_WRITE_CONFIRMATION=require"):
            await main.async_main(transport)
    finally:
        monkeypatch.delenv("MCP_ELICITATION_ENABLED", raising=False)
        reload_config()

    assert created == []
    assert calls["http"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["websocket", "sse"])
async def test_unrecognized_transport_stops_startup_before_the_server_is_built(startup_env, monkeypatch, transport):
    calls = _stub_runners(monkeypatch)
    created = _stub_server_factory(monkeypatch)

    with pytest.raises(ValueError, match="not supported"):
        await main.async_main(transport)

    assert created == []
    assert calls == {"http": [], "stdio": []}
