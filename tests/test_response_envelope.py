import json

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from entities.llm_config import LLM_FORMATTING_BASE_INSTRUCTIONS
from pkg.response_envelope import (
    ResponseEnvelopeMiddleware,
    apply_response_envelope,
    ensure_formatting_metadata,
)
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


def test_fastmcp_wrapped_array_uses_data_and_total():
    body = [{"id": 7, "observation_time": 10}]

    enveloped = apply_response_envelope({"result": body})

    assert set(enveloped) == {"data", "total", "_metadata"}
    assert enveloped["data"] == body
    assert enveloped["data"][0]["observation_time"] == 10
    assert enveloped["total"] == 1
    assert enveloped["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS
    assert "pagination" not in enveloped

    empty = apply_response_envelope({"result": []})
    assert empty["data"] == []
    assert empty["total"] == 0
    assert "result" not in empty


def test_objects_that_merely_contain_result_keep_their_fields():
    listed = apply_response_envelope({"result": [{"id": 1}], "success": "true"})
    assert listed["result"] == [{"id": 1}]
    assert listed["success"] == "true"
    assert "data" not in listed
    assert "total" not in listed
    assert listed["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS

    nested = apply_response_envelope({"result": {"observation_time": 1762774211000}})
    assert nested["result"]["observation_time"] == 1762774211000
    assert "data" not in nested
    assert "total" not in nested

    primitive = apply_response_envelope({"result": 5})
    assert primitive["result"] == 5
    assert "data" not in primitive


def test_non_dict_metadata_is_left_in_place():
    for existing in ("already set", ["keep"], 3, None):
        enveloped = ensure_formatting_metadata({"reply": {"observation_time": 10}, "_metadata": existing})
        assert enveloped["_metadata"] == existing
        assert enveloped["reply"]["observation_time"] == 10
        assert "formatting_instructions" not in enveloped

    parsed = json.loads(create_response({"error": "nope", "_metadata": "keep"}, is_error=True))
    assert parsed["_metadata"] == "keep"
    assert parsed["success"] == "false"
    assert parsed["error"] == "nope"


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


@pytest.mark.asyncio
async def test_middleware_envelopes_fastmcp_wrapped_openapi_arrays():
    body = [{"id": 7, "observation_time": 10}]

    async def call_next(context):
        return ToolResult(structured_content={"result": body})

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert set(parsed) == {"data", "total", "_metadata"}
    assert parsed["data"] == body
    assert parsed["data"][0]["observation_time"] == 10
    assert parsed["total"] == 1
    assert parsed["_metadata"]["formatting_instructions"] == LLM_FORMATTING_BASE_INSTRUCTIONS
    assert result.structured_content == parsed
    assert "pagination" not in parsed
    assert "result" not in parsed
