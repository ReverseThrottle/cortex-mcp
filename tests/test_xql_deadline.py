"""Issue #44: a deadline-cancelled XQL query keeps its execution_id. No live tenant."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import MiddlewareContext
from mcp.types import CallToolRequestParams

from config.config import get_config, reload_config
from pkg.call_deadline import ToolCallDeadlineMiddleware, record_call_recovery
from pkg.response_envelope import ResponseEnvelopeMiddleware
from usecase.custom_components import xql_query as xql_module
from usecase.custom_components.xql_query import (
    post_xql_get_query_results_stream,
    run_xql_query,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def short_deadline(monkeypatch):
    previous = os.environ.get("CORTEX_MCP_CALL_DEADLINE_S")
    monkeypatch.setenv("CORTEX_MCP_CALL_DEADLINE_S", "0.3")
    reload_config()
    yield
    if previous is None:
        monkeypatch.delenv("CORTEX_MCP_CALL_DEADLINE_S", raising=False)
    else:
        monkeypatch.setenv("CORTEX_MCP_CALL_DEADLINE_S", previous)
    reload_config()


class _PendingFetcher:
    """Starts one query, then answers PENDING forever."""

    def __init__(self) -> None:
        self.starts = 0
        self.polls = 0

    async def send_request(self, path, data=None, **kwargs):
        if "start_xql_query" in path:
            self.starts += 1
            return {"reply": "exec-44"}
        self.polls += 1
        return {"reply": {"status": "PENDING"}}


def _stub_fetcher(monkeypatch, fetcher) -> None:
    async def fake_get_fetcher(ctx):
        return fetcher

    monkeypatch.setattr(xql_module, "get_fetcher", fake_get_fetcher)


def _context(name: str = "run_xql_query") -> MiddlewareContext:
    return MiddlewareContext(
        message=CallToolRequestParams(name=name, arguments={}),
        method="tools/call",
        type="request",
    )


@pytest.mark.asyncio
async def test_deadline_cancelled_query_returns_execution_id(short_deadline, monkeypatch):
    fetcher = _PendingFetcher()
    _stub_fetcher(monkeypatch, fetcher)
    server = FastMCP("xql-deadline")
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.add_middleware(ToolCallDeadlineMiddleware())
    server.add_tool(run_xql_query)

    started = time.monotonic()
    async with Client(server) as mcp:
        result = await mcp.call_tool_mcp("run_xql_query", {"query": "dataset=xdr_data | limit 1"})
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert fetcher.starts == 1
    assert fetcher.polls >= 1
    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert body["success"] == "false"
    assert "Cortex API did not respond within" in body["error"]
    assert body["execution_id"] == "exec-44"
    assert "post_xql_get_query_results" in body["recovery_hint"]
    assert "post_xql_get_query_results_stream" in body["recovery_hint"]


@pytest.mark.asyncio
async def test_middleware_returns_recorded_recovery_fields_on_timeout(short_deadline):
    async def call_next(context):
        record_call_recovery(execution_id="exec-44", recovery_hint="use post_xql_get_query_results")
        await asyncio.sleep(5)
        return "ok"

    result = await ToolCallDeadlineMiddleware().on_call_tool(_context(), call_next)
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["execution_id"] == "exec-44"
    assert result.structured_content["recovery_hint"] == "use post_xql_get_query_results"


@pytest.mark.asyncio
async def test_recovery_fields_do_not_leak_into_the_next_call(short_deadline):
    async def slow_with_recovery(context):
        record_call_recovery(execution_id="exec-44")
        await asyncio.sleep(5)

    async def slow_plain(context):
        await asyncio.sleep(5)

    middleware = ToolCallDeadlineMiddleware()
    first = await middleware.on_call_tool(_context(), slow_with_recovery)
    second = await middleware.on_call_tool(_context("post_mth_child_get_all_reports"), slow_plain)
    assert first.structured_content["execution_id"] == "exec-44"
    assert "execution_id" not in second.structured_content
    assert second.structured_content["error"] == (
        "Cortex API did not respond within 0.3 s for post_mth_child_get_all_reports"
    )


def test_record_call_recovery_is_a_noop_without_middleware():
    # A direct tool invocation (unit tests, scripts) has no armed slot.
    record_call_recovery(execution_id="exec-44")


@pytest.mark.asyncio
async def test_poll_exhaustion_error_carries_execution_id(monkeypatch):
    fetcher = _PendingFetcher()
    _stub_fetcher(monkeypatch, fetcher)
    monkeypatch.setattr(xql_module, "_MAX_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(xql_module, "_POLL_INTERVAL_SECONDS", 0)

    result = await run_xql_query(None, "dataset=xdr_data | limit 1")
    body = json.loads(result)
    assert body["success"] == "false"
    assert fetcher.starts == 1
    assert body["execution_id"] == "exec-44"
    assert "post_xql_get_query_results" in body["recovery_hint"]


@pytest.mark.asyncio
async def test_stream_download_uses_the_call_budget(monkeypatch):
    seen = {}

    class StreamFetcher:
        async def send_request(self, path, data=None, **kwargs):
            seen["path"] = path
            seen.update(kwargs)
            return b'{"results": []}'

    _stub_fetcher(monkeypatch, StreamFetcher())
    result = await post_xql_get_query_results_stream(None, "stream-1")
    assert json.loads(result)["success"] == "true"
    assert seen["raw"] is True
    assert seen["timeout"] == get_config().cortex_call_deadline_s


def test_readme_documents_xql_deadline_recovery():
    readme = (ROOT / "README.md").read_text()
    assert "the error result carries the `execution_id` and a `recovery_hint`" in readme
    assert "gives its single download attempt the whole call budget" in readme
