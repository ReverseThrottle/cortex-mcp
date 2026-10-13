"""Upstream timeout and per-tool deadline. No live tenant and no credentials."""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx2 as httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from mcp.types import CallToolRequestParams

from config.config import Settings, reload_config
from entities.exceptions import (
    PAPIClientRequestError,
    PAPIConnectionError,
    PAPIDeadlineError,
)
from pkg.call_deadline import (
    ToolCallDeadlineMiddleware,
    is_slow_report_or_search,
    upstream_timeout_message,
)
from pkg.client import PAPIClient
from pkg.response_envelope import ResponseEnvelopeMiddleware

ROOT = Path(__file__).resolve().parents[1]

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "deadline", "version": "1"},
    "paths": {
        "/public_api/v1/mth/child/get_all_reports": {
            "post": {
                "operationId": "post_mth_child_get_all_reports",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _DeadlineConfig:
    def __init__(self, max_retries: int = 3, *, timeout_s: float = 20.0, deadline_s: float = 45.0) -> None:
        self.max_retries = max_retries
        self.http_response_error_message_max_size = 1000
        self.cortex_timeout_s = timeout_s
        self.cortex_call_deadline_s = deadline_s


def _context(name: str = "post_mth_child_get_all_reports") -> MiddlewareContext:
    return MiddlewareContext(
        message=CallToolRequestParams(name=name, arguments={}),
        method="tools/call",
        type="request",
    )


def _install(
    monkeypatch,
    *,
    max_retries: int = 3,
    timeout_s: float = 20.0,
    deadline_s: float = 45.0,
    clock: _Clock | None = None,
    pause: bool = True,
) -> None:
    monkeypatch.setattr(
        "pkg.client.get_config",
        lambda: _DeadlineConfig(max_retries, timeout_s=timeout_s, deadline_s=deadline_s),
    )
    if clock is not None:
        monkeypatch.setattr("pkg.call_deadline.time.monotonic", clock.monotonic)
    if pause:

        async def _no_wait(self, attempt: int) -> None:
            return None

        monkeypatch.setattr(PAPIClient, "_pause_before_retry", _no_wait)


def _client(handler, monkeypatch, **kwargs) -> PAPIClient:
    _install(monkeypatch, **kwargs)
    return PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(handler),
        timeout=kwargs.get("timeout_s", 20.0),
    )


@pytest.fixture
def restored_timeout_env(monkeypatch):
    previous = {
        "CORTEX_MCP_TIMEOUT_S": os.environ.get("CORTEX_MCP_TIMEOUT_S"),
        "CORTEX_MCP_CALL_DEADLINE_S": os.environ.get("CORTEX_MCP_CALL_DEADLINE_S"),
    }

    def apply(*, timeout_s: str, deadline_s: str) -> Settings:
        monkeypatch.setenv("CORTEX_MCP_TIMEOUT_S", timeout_s)
        monkeypatch.setenv("CORTEX_MCP_CALL_DEADLINE_S", deadline_s)
        return reload_config()

    yield apply

    for name, value in previous.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reload_config()


def test_timeout_defaults_sit_under_the_mcp_client_timeout():
    timeout_s = Settings.model_fields["cortex_timeout_s"].default
    deadline_s = Settings.model_fields["cortex_call_deadline_s"].default
    assert timeout_s == 20.0
    assert deadline_s == 45.0
    assert deadline_s < 60
    assert timeout_s < deadline_s

    readme = (ROOT / "README.md").read_text()
    example = (ROOT / ".env.example").read_text()
    assert "| `CORTEX_MCP_TIMEOUT_S` | `20` |" in readme
    assert "| `CORTEX_MCP_CALL_DEADLINE_S` | `45` |" in readme
    assert "MCP_REQUEST_TIMEOUT_MS" in readme
    assert "CORTEX_MCP_TIMEOUT_S=20" in example
    assert "CORTEX_MCP_CALL_DEADLINE_S=45" in example
    assert "MCP_REQUEST_TIMEOUT_MS=60000" in example


def test_timeout_settings_load_from_the_environment(monkeypatch):
    monkeypatch.setenv("CORTEX_MCP_TIMEOUT_S", "12")
    monkeypatch.setenv("CORTEX_MCP_CALL_DEADLINE_S", "30")
    loaded = Settings()
    assert loaded.cortex_timeout_s == 12
    assert loaded.cortex_call_deadline_s == 30


@pytest.mark.parametrize(
    ("url", "slow"),
    [
        ("https://api.example.invalid/public_api/v1/mth/child/get_all_reports", True),
        ("https://api.example.invalid/public_api/v1/case/search", True),
        ("https://api.example.invalid/public_api/v1/issue_exceptions/search/", True),
        ("https://api.example.invalid/public_api/v1/mth/child/get_reports_by_statuses", True),
        ("https://api.example.invalid/public_api/v1/mcp/download/", False),
        ("https://api.example.invalid/public_api/v1/xql/get_query_results_stream", False),
        ("https://api.example.invalid/public_api/v1/endpoints/get_endpoint", False),
    ],
)
def test_slow_report_and_search_paths(url, slow):
    assert is_slow_report_or_search(url) is slow


def test_timeout_message_names_the_endpoint_and_the_limit():
    message = upstream_timeout_message(
        "https://api.example.invalid/public_api/v1/mth/child/get_all_reports",
        20,
    )
    assert message == "Cortex API did not respond within 20 s for mth/child/get_all_reports"


@pytest.mark.asyncio
async def test_report_read_timeout_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    client = _client(handler, monkeypatch)
    with pytest.raises(PAPIDeadlineError, match="within 20 s for mth/child/get_all_reports"):
        await client.request("POST", "/public_api/v1/mth/child/get_all_reports", json={"request_data": {}})
    await client.aclose()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_connect_timeout_on_a_report_still_retries(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("down")

    client = _client(handler, monkeypatch)
    with pytest.raises(PAPIConnectionError, match="Request timeout"):
        await client.request("POST", "/public_api/v1/mth/child/get_all_reports")
    await client.aclose()
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_connect_error_on_a_report_still_retries(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    client = _client(handler, monkeypatch)
    with pytest.raises(PAPIConnectionError, match="Failed to connect"):
        await client.request("POST", "/public_api/v1/mth/child/get_all_reports")
    await client.aclose()
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_read_timeout_on_other_endpoints_still_retries(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    client = _client(handler, monkeypatch)
    with pytest.raises(PAPIConnectionError, match="Request timeout"):
        await client.request("POST", "/public_api/v1/endpoints/get_endpoint")
    await client.aclose()
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_read_timeout_retries_stop_at_the_deadline(monkeypatch):
    clock = _Clock()
    calls = {"n": 0}
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        read_timeout = request.extensions["timeout"]["read"]
        timeouts.append(read_timeout)
        clock.advance(read_timeout)
        raise httpx.ReadTimeout("slow")

    client = _client(handler, monkeypatch, clock=clock)
    with pytest.raises(PAPIDeadlineError, match="within 45 s for endpoints/get_endpoint"):
        await client.request("POST", "/public_api/v1/endpoints/get_endpoint")
    await client.aclose()

    assert calls["n"] == 3
    assert timeouts == [20.0, 20.0, 5.0]


@pytest.mark.asyncio
async def test_attempt_timeout_cannot_exceed_the_deadline(monkeypatch):
    seen: list[float] = []
    clock = _Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"ok": True})

    _install(monkeypatch, deadline_s=45.0, timeout_s=20.0, clock=clock)
    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        timeout=300,
        transport=httpx.MockTransport(handler),
    )
    result = await client.request("POST", "/public_api/v1/endpoints/get_endpoint", timeout=120)
    await client.aclose()
    assert result == {"ok": True}
    assert seen == [45.0]


@pytest.mark.asyncio
async def test_429_still_retries_until_the_deadline(monkeypatch):
    clock = _Clock()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        clock.advance(30)
        if calls["n"] < 3:
            return httpx.Response(429, json={"err_code": 1})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, monkeypatch, clock=clock)
    with pytest.raises(PAPIClientRequestError) as exc:
        await client.request("POST", "/public_api/v1/endpoints/get_endpoint")
    await client.aclose()
    assert calls["n"] == 2
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_backoff_cannot_sleep_past_the_deadline(monkeypatch):
    clock = _Clock()
    calls = {"n": 0}
    slept: list[float] = []
    monkeypatch.setattr("pkg.client.random.uniform", lambda start, end: end)

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    monkeypatch.setattr("pkg.client.asyncio.sleep", _sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        clock.advance(40)
        raise httpx.ConnectError("down")

    client = _client(handler, monkeypatch, clock=clock, pause=False)
    with pytest.raises(PAPIDeadlineError, match="within 45 s for endpoints/get_endpoint"):
        await client.request("POST", "/public_api/v1/endpoints/get_endpoint")
    await client.aclose()
    assert calls["n"] == 2
    assert slept == [0.5]


@pytest.mark.asyncio
async def test_middleware_unwraps_a_wrapped_timeout_into_an_iserror_result():
    async def call_next(context):
        raise ToolError(
            "Error calling tool 'post_mth_child_get_all_reports': "
            "Cortex API did not respond within 20 s for mth/child/get_all_reports"
        )

    result = await ToolCallDeadlineMiddleware().on_call_tool(_context(), call_next)
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["success"] == "false"
    assert result.structured_content["error"] == (
        "Cortex API did not respond within 20 s for mth/child/get_all_reports"
    )


@pytest.mark.asyncio
async def test_middleware_leaves_other_tool_errors_alone():
    async def call_next(context):
        raise ToolError("schema exploded")

    with pytest.raises(ToolError, match="schema exploded"):
        await ToolCallDeadlineMiddleware().on_call_tool(_context(), call_next)


@pytest.mark.asyncio
async def test_hung_tool_returns_iserror_before_the_client_timeout(restored_timeout_env):
    restored_timeout_env(timeout_s="20", deadline_s="0.3")
    server = FastMCP("deadline")
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.add_middleware(ToolCallDeadlineMiddleware())

    async def post_mth_child_get_all_reports() -> str:
        await asyncio.sleep(5)
        return "ok"

    server.add_tool(post_mth_child_get_all_reports)
    started = time.monotonic()
    async with Client(server) as mcp:
        result = await mcp.call_tool_mcp("post_mth_child_get_all_reports", {})
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert body["success"] == "false"
    assert "Cortex API did not respond within" in body["error"]
    assert "post_mth_child_get_all_reports" in body["error"]


@pytest.mark.asyncio
async def test_catalog_report_read_timeout_is_an_iserror_tool_result(restored_timeout_env):
    restored_timeout_env(timeout_s="20", deadline_s="45")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    api = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(handler),
    )
    server = FastMCP.from_openapi(_SPEC, api)
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.add_middleware(ToolCallDeadlineMiddleware())
    try:
        async with Client(server) as mcp:
            result = await mcp.call_tool_mcp("post_mth_child_get_all_reports", {})
    finally:
        await api.aclose()

    assert calls["n"] == 1
    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert body["success"] == "false"
    assert body["error"] == "Cortex API did not respond within 20 s for mth/child/get_all_reports"
    assert "Error calling tool" not in result.content[0].text
