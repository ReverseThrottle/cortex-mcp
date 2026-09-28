import json

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from entities.llm_config import LLM_FORMATTING_BASE_INSTRUCTIONS
from pkg.response_envelope import ResponseEnvelopeMiddleware, apply_response_envelope
from pkg.util import create_response


def test_create_response_keeps_cortex_fields_and_adds_formatting_metadata():
    payload = {"reply": {"observation_time": 1762774211000, "severity": "high"}}

    parsed = json.loads(create_response(payload))

    assert parsed["reply"]["observation_time"] == 1762774211000
    assert parsed["reply"]["severity"] == "high"
    assert parsed["success"] == "true"
    assert parsed["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS
    assert "data" not in parsed
    assert "total" not in parsed
    assert "pagination" not in parsed


def test_create_response_preserves_tool_formatting_instructions():
    parsed = json.loads(
        create_response(
            {
                "_metadata": {"formatting_instructions": "keep this", "source": "get_issues"},
                "reply": [],
            },
            is_error=False,
        )
    )

    assert parsed["_metadata"]["formatting_instructions"] == "keep this"
    assert parsed["_metadata"]["source"] == "get_issues"
    assert parsed["success"] == "true"


def test_array_results_use_data_and_total():
    enveloped = apply_response_envelope([{"id": 7, "observation_time": 10}])

    assert enveloped["data"] == [{"id": 7, "observation_time": 10}]
    assert enveloped["total"] == 1
    assert enveloped["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS
    assert "pagination" not in enveloped


def _context() -> MiddlewareContext:
    return MiddlewareContext(
        message=CallToolRequestParams(name="get_issues", arguments={}),
        method="tools/call",
        type="request",
    )


@pytest.mark.asyncio
async def test_middleware_envelopes_object_results_without_rewriting_values():
    async def call_next(context):
        body = {"reply": {"observation_time": 1762774211000}}
        return ToolResult(content=body, structured_content=body)

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert parsed["reply"]["observation_time"] == 1762774211000
    assert parsed["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS
    assert result.structured_content["reply"]["observation_time"] == 1762774211000
    assert "pagination" not in parsed


@pytest.mark.asyncio
async def test_middleware_wraps_json_arrays_and_leaves_plain_text():
    async def call_next(context):
        return ToolResult(content=[{"id": 3}])

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert parsed["data"] == [{"id": 3}]
    assert parsed["total"] == 1
    assert result.structured_content["total"] == 1

    async def plain(context):
        return ToolResult(content="not json")

    untouched = await ResponseEnvelopeMiddleware().on_call_tool(_context(), plain)
    assert untouched.content[0].text == "not json"
    assert untouched.structured_content is None
