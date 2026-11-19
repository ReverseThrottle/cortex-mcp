"""Formatting guidance is sent once at initialize, not inside every tool result."""

import json
import os

import httpx2 as httpx
import pytest
from fastmcp import Client, FastMCP

from config.config import reload_config
from entities.llm_config import LLM_FORMATTING_BASE_INSTRUCTIONS
from main import openapi_route_map
from pkg.client import PAPIClient
from pkg.input_validation import JsonSchemaInputMiddleware
from pkg.response_envelope import ResponseEnvelopeMiddleware, apply_response_envelope
from pkg.tool_presentation import annotate_openapi_component
from pkg.tool_rate_limit import ToolCallRateLimitMiddleware
from pkg.util import bundle_openapi_from_folders, create_response
from pkg.write_confirmation import WriteConfirmationMiddleware
from service.cortex_mcp.server import create_mcp_server
from usecase.base_module import BaseModule
from version import __version__

# Long lines from the guidance. Short tokens such as "json" also appear in
# ordinary payloads, so the check is the injected prose, not every substring.
_GUIDANCE_MARKERS = tuple(
    line.strip() for line in LLM_FORMATTING_BASE_INSTRUCTIONS.splitlines() if len(line.strip()) >= 40
)


def _has_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_has_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_has_key(item, key) for item in value)
    return False


def _assert_no_formatting_instructions(text: str) -> dict:
    assert "formatting_instructions" not in text
    for marker in _GUIDANCE_MARKERS:
        assert marker not in text, marker
    parsed = json.loads(text)
    assert not _has_key(parsed, "formatting_instructions")
    return parsed


class _Tools(BaseModule):
    def register_tools(self):
        self._add_tool(self.echo, side_effect="read")
        self._add_tool(self.needs_int, side_effect="read")
        self._add_tool(self.change_state, side_effect="write")

    def register_resources(self):
        return None

    async def echo(self) -> str:
        """Side effects: none. Echo a successful envelope."""
        return create_response({"reply": []})

    async def needs_int(self, value: int) -> str:
        """Side effects: none. Require an integer argument."""
        return create_response({"reply": value})

    async def change_state(self) -> str:
        """Side effects: this operation changes Cortex tenant state. Must not run when confirmation is missing."""
        return create_response({"reply": "ran"})


def _server(*, burst: int = 20, rate: float = 10) -> FastMCP:
    server = create_mcp_server("test-key", "1")
    server.add_middleware(
        ToolCallRateLimitMiddleware(
            max_requests_per_second=rate,
            burst_capacity=burst,
            global_limit=True,
        )
    )
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.add_middleware(JsonSchemaInputMiddleware())
    server.add_middleware(WriteConfirmationMiddleware())
    _Tools(server).register_tools()
    return server


@pytest.fixture
def inline_formatting(monkeypatch):
    previous = os.environ.get("MCP_INLINE_FORMATTING_INSTRUCTIONS")
    elicitation = os.environ.get("MCP_ELICITATION_ENABLED")

    def apply(*, inline: bool, confirm: bool = False):
        monkeypatch.setenv("MCP_INLINE_FORMATTING_INSTRUCTIONS", "true" if inline else "false")
        monkeypatch.setenv("MCP_ELICITATION_ENABLED", "true" if confirm else "false")
        return reload_config()

    apply(inline=False)
    yield apply

    for name, value in (
        ("MCP_INLINE_FORMATTING_INSTRUCTIONS", previous),
        ("MCP_ELICITATION_ENABLED", elicitation),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reload_config()


@pytest.mark.asyncio
async def test_initialize_instructions_carry_the_guidance_once(inline_formatting):
    inline_formatting(inline=False)
    server = create_mcp_server("test-key", "1")
    assert server.instructions is not None
    assert "NEVER fabricate" in server.instructions

    async with Client(server, mode="legacy") as client:
        initialized = client.initialize_result

    assert initialized is not None
    assert initialized.instructions is not None
    assert LLM_FORMATTING_BASE_INSTRUCTIONS.strip() in initialized.instructions
    assert "NEVER fabricate" in initialized.instructions

    async with Client(server, mode="auto") as client:
        assert client.protocol_version == "2026-07-28"
        assert client.initialize_result is None
        discovered = client.session.discover_result
        assert discovered is not None
        assert discovered.instructions is not None
        assert LLM_FORMATTING_BASE_INSTRUCTIONS.strip() in discovered.instructions
        assert client.instructions == discovered.instructions


@pytest.mark.asyncio
async def test_tool_results_omit_formatting_instructions(inline_formatting):
    inline_formatting(inline=False, confirm=True)
    server = _server()
    async with Client(server, mode="legacy") as client:
        success = await client.call_tool_mcp("echo", {})
        invalid = await client.call_tool_mcp("needs_int", {"value": "nope"})
        refused = await client.call_tool_mcp("change_state", {})

    assert success.is_error is False
    body = _assert_no_formatting_instructions(success.content[0].text)
    assert body["reply"] == []
    assert body["success"] == "true"
    assert len(success.content[0].text.encode()) < 200

    assert invalid.is_error is True
    invalid_body = _assert_no_formatting_instructions(invalid.content[0].text)
    assert invalid_body["success"] == "false"
    assert "Invalid arguments" in invalid_body["error"]

    assert refused.is_error is True
    refused_body = _assert_no_formatting_instructions(refused.content[0].text)
    assert refused_body["success"] == "false"
    assert "was not run" in refused_body["error"]
    assert refused_body.get("reply") != "ran"


@pytest.mark.asyncio
async def test_rate_limit_refusal_omits_formatting_instructions(inline_formatting):
    inline_formatting(inline=False)
    server = _server(burst=1, rate=0.001)
    async with Client(server, mode="legacy") as client:
        await client.call_tool_mcp("echo", {})
        limited = await client.call_tool_mcp("echo", {})

    assert limited.is_error is True
    body = _assert_no_formatting_instructions(limited.content[0].text)
    assert body["success"] == "false"
    assert "rate limit" in body["error"].lower()


def test_create_response_leaves_the_callers_dict_unchanged(inline_formatting):
    for inline in (False, True):
        inline_formatting(inline=inline)
        data = {"reply": []}
        parsed = json.loads(create_response(data, is_error=True))
        assert parsed["success"] == "false"
        assert ("_metadata" in parsed) is inline
        assert data == {"reply": []}


def test_opt_in_restores_inline_formatting_on_envelopes(inline_formatting):
    inline_formatting(inline=True)
    parsed = json.loads(create_response({"reply": []}))
    assert parsed["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS

    kept = json.loads(
        create_response({"_metadata": {"formatting_instructions": "keep this", "source": "get_issues"}, "reply": []})
    )
    assert kept["_metadata"]["formatting_instructions"] == "keep this"
    assert kept["_metadata"]["source"] == "get_issues"

    enveloped = apply_response_envelope([{"id": 7}])
    assert enveloped["data"] == [{"id": 7}]
    assert enveloped["total"] == 1
    assert enveloped["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS


@pytest.mark.asyncio
async def test_opt_in_restores_inline_formatting_on_tool_calls(inline_formatting):
    inline_formatting(inline=True, confirm=True)
    server = _server()
    limited_server = _server(burst=1, rate=0.001)
    async with Client(server, mode="legacy") as client:
        success = await client.call_tool_mcp("echo", {})
        invalid = await client.call_tool_mcp("needs_int", {"value": "nope"})
        refused = await client.call_tool_mcp("change_state", {})
    async with Client(limited_server, mode="legacy") as client:
        await client.call_tool_mcp("echo", {})
        limited = await client.call_tool_mcp("echo", {})

    for result in (success, invalid, refused, limited):
        body = json.loads(result.content[0].text)
        assert body["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS


@pytest.mark.asyncio
async def test_get_brokers_empty_reply_stays_under_200_bytes(inline_formatting):
    inline_formatting(inline=False)
    spec = bundle_openapi_from_folders()
    brokers = {
        "openapi": spec.get("openapi", "3.0.0"),
        "info": spec.get("info") or {"title": "brokers", "version": "0"},
        "paths": {"/public_api/v1/brokers/": spec["paths"]["/public_api/v1/brokers/"]},
    }
    if "components" in spec:
        brokers["components"] = spec["components"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.rstrip("/").endswith("/public_api/v1/brokers")
        return httpx.Response(200, json={"reply": []})

    api = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "1",
        transport=httpx.MockTransport(handler),
    )
    catalog = FastMCP.from_openapi(
        brokers,
        api,
        route_map_fn=openapi_route_map,
        mcp_component_fn=annotate_openapi_component,
        strict_input_validation=True,
        version=__version__,
    )
    server = create_mcp_server("tenant-secret", "1")
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.mount(catalog)

    async with Client(server, mode="legacy") as client:
        result = await client.call_tool_mcp("get_brokers", {})

    assert result.is_error is False
    text = result.content[0].text
    body = _assert_no_formatting_instructions(text)
    assert body["reply"] == []
    assert len(text.encode()) < 200

    inline_formatting(inline=True)
    async with Client(server, mode="legacy") as client:
        opted = await client.call_tool_mcp("get_brokers", {})

    opted_text = opted.content[0].text
    opted_body = json.loads(opted_text)
    assert opted_body["reply"] == []
    assert opted_body["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS
    assert len(opted_text.encode()) > 200
