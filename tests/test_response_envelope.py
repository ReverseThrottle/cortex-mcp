import json

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import InputRequiredToolResult, ToolResult
from mcp.types import (
    AudioContent,
    CallToolRequestParams,
    EmbeddedResource,
    ImageContent,
    InputRequiredResult,
    TextContent,
    TextResourceContents,
)

from pkg.response_envelope import (
    ResponseEnvelopeMiddleware,
    _bounds,
    _fit_count,
    apply_response_envelope,
    ensure_formatting_metadata,
    envelope_tool_result,
    paging_from_arguments,
)
from pkg.util import create_response
from usecase.base_module import BaseModule


def test_create_response_keeps_cortex_fields_and_adds_formatting_metadata():
    payload = {"reply": {"observation_time": 1762774211000, "severity": "high"}}

    parsed = json.loads(create_response(payload))

    assert parsed["reply"]["observation_time"] == 1762774211000
    assert parsed["reply"]["severity"] == "high"
    assert parsed["success"] == "true"
    assert "_metadata" not in parsed
    assert "formatting_instructions" not in parsed
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
    assert "_metadata" not in enveloped
    assert "pagination" not in enveloped


def test_fastmcp_wrapped_array_uses_data_and_total():
    body = [{"id": 7, "observation_time": 10}]

    enveloped = apply_response_envelope({"result": body})

    assert set(enveloped) == {"data", "total"}
    assert enveloped["data"] == body
    assert enveloped["data"][0]["observation_time"] == 10
    assert enveloped["total"] == 1
    assert "_metadata" not in enveloped
    assert "pagination" not in enveloped

    empty = apply_response_envelope({"result": []})
    assert empty["data"] == []
    assert empty["total"] == 0
    assert "result" not in empty


def test_fastmcp_wrapped_json_string_mixes_object_fields():
    body = {
        "reply": {"observation_time": 1762774211000},
        "success": "true",
        "_metadata": {"formatting_instructions": "keep this", "source": "get_issues"},
    }

    enveloped = apply_response_envelope({"result": json.dumps(body)})

    assert enveloped["reply"]["observation_time"] == 1762774211000
    assert enveloped["success"] == "true"
    assert enveloped["_metadata"]["formatting_instructions"] == "keep this"
    assert enveloped["_metadata"]["source"] == "get_issues"
    assert "result" not in enveloped
    assert "data" not in enveloped
    assert "total" not in enveloped
    assert "pagination" not in enveloped

    plain = apply_response_envelope({"result": "not json"})
    assert plain["result"] == "not json"
    assert "_metadata" not in plain


def test_objects_that_merely_contain_result_keep_their_fields():
    listed = apply_response_envelope({"result": [{"id": 1}], "success": "true"})
    assert listed["result"] == [{"id": 1}]
    assert listed["success"] == "true"
    assert "data" not in listed
    assert "total" not in listed
    assert "_metadata" not in listed

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


def _context(arguments: dict | None = None) -> MiddlewareContext:
    return MiddlewareContext(
        message=CallToolRequestParams(name="get_issues", arguments={} if arguments is None else arguments),
        method="tools/call",
        type="request",
    )


@pytest.mark.asyncio
async def test_envelope_leaves_an_input_required_result_unchanged():
    pending = InputRequiredToolResult(InputRequiredResult(request_state="pending"))

    async def call_next(context):
        return pending

    assert await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next) is pending


@pytest.mark.asyncio
async def test_middleware_envelopes_object_results_without_rewriting_values():
    async def call_next(context):
        body = {"reply": {"observation_time": 1762774211000}}
        return ToolResult(content=body, structured_content=body)

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert parsed["reply"]["observation_time"] == 1762774211000
    assert "_metadata" not in parsed
    assert result.structured_content["reply"]["observation_time"] == 1762774211000
    assert "pagination" not in parsed


@pytest.mark.asyncio
async def test_middleware_wraps_json_arrays_and_plain_text():
    async def call_next(context):
        return ToolResult(content=[{"id": 3}])

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert parsed["data"] == [{"id": 3}]
    assert parsed["total"] == 1
    assert result.structured_content["total"] == 1

    async def plain(context):
        return ToolResult(content="not json")

    wrapped = await ResponseEnvelopeMiddleware().on_call_tool(_context(), plain)
    parsed_text = json.loads(wrapped.content[0].text)
    assert parsed_text["content"] == "not json"
    assert wrapped.structured_content["content"] == "not json"
    assert "_metadata" not in parsed_text


@pytest.mark.asyncio
async def test_middleware_envelopes_fastmcp_wrapped_openapi_arrays():
    body = [{"id": 7, "observation_time": 10}]

    async def call_next(context):
        return ToolResult(structured_content={"result": body})

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert set(parsed) == {"data", "total"}
    assert parsed["data"] == body
    assert parsed["data"][0]["observation_time"] == 10
    assert parsed["total"] == 1
    assert "_metadata" not in parsed
    assert result.structured_content == parsed
    assert "pagination" not in parsed
    assert "result" not in parsed


@pytest.mark.asyncio
async def test_middleware_mixes_handwritten_string_results_into_the_object():
    text = create_response(
        {
            "_metadata": {"formatting_instructions": "keep this", "source": "get_issues"},
            "reply": {"observation_time": 1762774211000},
        }
    )

    async def call_next(context):
        return ToolResult(content=text, structured_content={"result": text})

    result = await ResponseEnvelopeMiddleware().on_call_tool(_context(), call_next)
    parsed = json.loads(result.content[0].text)

    assert parsed["reply"]["observation_time"] == 1762774211000
    assert parsed["success"] == "true"
    assert parsed["_metadata"]["formatting_instructions"] == "keep this"
    assert parsed["_metadata"]["source"] == "get_issues"
    assert result.structured_content == parsed
    assert "result" not in parsed
    assert "data" not in parsed
    assert "pagination" not in parsed


class _RecordingModule(BaseModule):
    def __init__(self, mcp: FastMCP, fn):
        super().__init__(mcp)
        self._fn = fn

    def register_tools(self):
        self._add_tool(self._fn, side_effect="read")

    def register_resources(self):
        return None


class _RefuseMiddleware(Middleware):
    """Stand in for write confirmation: return the refusal without running the tool."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> ToolResult:
        if context.message.name == "refuse_write":
            return ToolResult(content=create_response(data={"error": "not confirmed"}, is_error=True))
        return await call_next(context)


@pytest.mark.asyncio
async def test_handwritten_tool_call_returns_the_mixed_object():
    async def get_issues_shaped() -> str:
        return create_response(
            {
                "_metadata": {"formatting_instructions": "keep this", "source": "get_issues"},
                "reply": {"observation_time": 1762774211000},
            }
        )

    async def refuse_write() -> str:
        return create_response(data={"error": "should not run"})

    mcp = FastMCP("envelope-test")
    # Same order as the server: the envelope is added first and sees the refusal.
    mcp.add_middleware(ResponseEnvelopeMiddleware())
    mcp.add_middleware(_RefuseMiddleware())
    _RecordingModule(mcp, get_issues_shaped).register_tools()
    _RecordingModule(mcp, refuse_write).register_tools()

    issues_tool = await mcp.get_tool("get_issues_shaped")
    refuse_tool = await mcp.get_tool("refuse_write")
    assert issues_tool.output_schema == {"type": "object", "additionalProperties": True}
    assert refuse_tool.output_schema == {"type": "object", "additionalProperties": True}

    async with Client(mcp) as client:
        success = await client.call_tool_mcp("get_issues_shaped", {})
        refused = await client.call_tool_mcp("refuse_write", {})

    assert success.is_error is False
    parsed = json.loads(success.content[0].text)
    assert parsed["reply"]["observation_time"] == 1762774211000
    assert parsed["success"] == "true"
    assert parsed["_metadata"]["formatting_instructions"] == "keep this"
    assert parsed["_metadata"]["source"] == "get_issues"
    assert success.structured_content == parsed
    assert "result" not in parsed

    assert refused.is_error is True
    refusal = json.loads(refused.content[0].text)
    assert refusal["error"] == "not confirmed"
    assert refusal["success"] == "false"
    assert "_metadata" not in refusal
    assert "formatting_instructions" not in refusal
    assert refused.structured_content == refusal
    assert "result" not in refusal


def test_long_lists_keep_a_default_page_and_flag_truncation():
    rows = [{"id": index} for index in range(150)]

    enveloped = apply_response_envelope(rows)

    assert enveloped["data"] == rows[:100]
    assert enveloped["total"] == 150
    assert enveloped["returned"] == 100
    assert enveloped["truncated"] is True
    assert "_metadata" not in enveloped


def test_search_window_slices_reply_data_without_dropping_sibling_fields():
    rows = [{"id": index} for index in range(21679)]

    enveloped = apply_response_envelope(
        {"reply": {"data": rows, "result_count": 21679}},
        search_from=0,
        search_to=1,
    )

    assert enveloped["reply"]["data"] == [{"id": 0}]
    assert enveloped["reply"]["result_count"] == 21679
    assert enveloped["truncated"] is True
    assert enveloped["returned"] == 1
    assert enveloped["total"] == 21679
    assert rows[1]["id"] == 1


def test_reply_list_uses_the_same_default_page():
    datasets = [{"dataset_name": f"d{index}"} for index in range(150)]

    enveloped = apply_response_envelope({"reply": datasets})

    assert enveloped["reply"] == datasets[:100]
    assert enveloped["truncated"] is True
    assert enveloped["returned"] == 100
    assert enveloped["total"] == 150


def test_byte_cap_shortens_a_page():
    rows = [{"id": index, "blob": "x" * 1000} for index in range(20)]

    enveloped = apply_response_envelope(rows, max_bytes=8000)
    encoded = json.dumps(enveloped, indent=2, ensure_ascii=False).encode("utf-8")

    assert enveloped["truncated"] is True
    assert enveloped["total"] == 20
    assert enveloped["returned"] == len(enveloped["data"])
    assert enveloped["returned"] < 20
    assert len(encoded) <= 8000


def _rowless_results_map(case_count: int = 40) -> dict:
    """update_case's per-case results map: no reply/data row list anywhere."""
    return {
        "results": {
            str(case_id): {"reply": {"notes": f"case {case_id} resolved ✓ " + "x" * 200, "status": "Resolved"}}
            for case_id in range(case_count)
        }
    }


def test_rowless_over_budget_payload_is_capped_to_a_preview():
    payload = _rowless_results_map()
    max_bytes = 4000
    raw = json.dumps(payload, indent=2, ensure_ascii=False)
    assert len(raw.encode("utf-8")) > max_bytes

    enveloped = apply_response_envelope(payload, max_bytes=max_bytes)

    assert set(enveloped) == {"truncated", "returned", "preview"}
    assert enveloped["truncated"] is True
    assert enveloped["returned"] == 0
    preview = enveloped["preview"]
    assert preview
    assert raw.startswith(preview)
    assert len(preview) < len(raw)
    encoded = json.dumps(enveloped, indent=2, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= max_bytes


def test_rowless_under_budget_payload_is_returned_unchanged():
    payload = _rowless_results_map(case_count=2)

    enveloped = apply_response_envelope(payload, max_bytes=4000)

    assert enveloped == payload
    assert "truncated" not in enveloped
    assert "returned" not in enveloped
    assert "preview" not in enveloped


def test_fastmcp_wrapped_rowless_results_map_is_preview_truncated():
    # update_case returns create_response(data={"results": {...}}); FastMCP
    # stores that -> str text under "result" before the envelope middleware runs.
    text = create_response(data=_rowless_results_map())
    enveloped = apply_response_envelope({"result": text}, max_bytes=4000)

    assert set(enveloped) == {"truncated", "returned", "preview"}
    assert enveloped["truncated"] is True
    assert enveloped["returned"] == 0
    raw = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    assert enveloped["preview"]
    assert raw.startswith(enveloped["preview"])
    assert len(enveloped["preview"]) < len(raw)
    encoded = json.dumps(enveloped, indent=2, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 4000


@pytest.mark.asyncio
async def test_middleware_slices_a_controls_page_from_the_tool_arguments():
    rows = [{"id": index} for index in range(30)]

    async def call_next(context):
        body = {"reply": {"data": rows, "result_count": len(rows)}}
        return ToolResult(structured_content=body)

    result = await ResponseEnvelopeMiddleware().on_call_tool(
        _context({"request_data": {"search_from": 0, "search_to": 1}}),
        call_next,
    )
    parsed = json.loads(result.content[0].text)

    assert parsed["reply"]["data"] == [{"id": 0}]
    assert parsed["reply"]["result_count"] == 30
    assert parsed["truncated"] is True
    assert parsed["returned"] == 1
    assert parsed["total"] == 30
    assert result.structured_content["reply"]["data"] == [{"id": 0}]


def test_already_paged_reply_lists_are_not_indexed_again():
    endpoints = [{"id": index} for index in range(100)]
    cases = [{"id": index} for index in range(30)]
    issues = [{"id": index} for index in range(10)]

    endpoint_page = apply_response_envelope(
        {"reply": {"endpoints": endpoints}},
        search_from=100,
        search_to=200,
    )
    case_page = apply_response_envelope(
        {"reply": {"DATA": cases}},
        search_from=30,
        search_to=60,
    )
    issue_page = apply_response_envelope(
        {"reply": {"issues": issues}},
        search_from=10,
        search_to=20,
    )
    first_page = apply_response_envelope(
        {"reply": {"issues": issues}},
        search_from=0,
        search_to=10,
    )

    assert endpoint_page["reply"]["endpoints"] == endpoints
    assert "truncated" not in endpoint_page
    assert case_page["reply"]["DATA"] == cases
    assert "truncated" not in case_page
    assert issue_page["reply"]["issues"] == issues
    assert "truncated" not in issue_page
    assert first_page["reply"]["issues"] == issues
    assert "truncated" not in first_page


def test_named_reply_list_without_bounds_keeps_every_row():
    devices = [{"id": index} for index in range(150)]

    enveloped = apply_response_envelope({"reply": {"devices": devices}})

    assert enveloped["reply"]["devices"] == devices
    assert "truncated" not in enveloped


def test_named_reply_list_longer_than_the_window_is_a_corpus():
    endpoints = [{"id": index} for index in range(150)]

    enveloped = apply_response_envelope(
        {"reply": {"endpoints": endpoints}},
        search_from=0,
        search_to=1,
    )

    assert enveloped["reply"]["endpoints"] == [{"id": 0}]
    assert enveloped["truncated"] is True
    assert enveloped["returned"] == 1
    assert enveloped["total"] == 150


# --- Edge contracts deferred by #74's scope note (issue #78). ---
# Each test asserts observed output, locking a reachable branch that had zero
# coverage. Defensive/unreachable lines (167, 174, 182, 240, 422) are out of scope.


def test_fastmcp_wrapped_json_array_string_stays_wrapped():
    # A JSON *array* string under "result" is not an object envelope: it stays
    # wrapped rather than being parsed into data/total.
    enveloped = apply_response_envelope({"result": "[1, 2]"})
    assert enveloped == {"result": "[1, 2]"}
    assert "data" not in enveloped
    assert "total" not in enveloped


def test_inverted_search_window_clamps_to_an_empty_page():
    rows = [{"id": index} for index in range(150)]
    enveloped = apply_response_envelope(rows, search_from=50, search_to=10)
    assert enveloped["data"] == []
    assert enveloped["total"] == 150
    assert enveloped["returned"] == 0
    assert enveloped["truncated"] is True


def test_bounds_clamp_an_inverted_window_to_zero_width():
    # The clamp's observable contract is the bounds pair itself: end becomes
    # start, so the window has zero width rather than a negative one.
    assert _bounds(50, 10) == (50, 50)
    assert _bounds(0, -5) == (0, 0)
    assert _bounds(-3, None) == (0, 100)
    assert _bounds(None, None) == (0, 100)


def test_paging_rejects_non_integer_and_boolean_bounds():
    assert paging_from_arguments({"search_from": "10", "search_to": True}) == (None, None)
    assert paging_from_arguments({"search_from": 1.5, "search_to": None}) == (None, None)
    assert paging_from_arguments({"search_from": 3, "search_to": 9}) == (3, 9)


def test_paging_from_non_dict_arguments_is_empty():
    # Argument-less tools reach the middleware with arguments=None.
    for arguments in (None, "search_from=1", [], 5, True):
        assert paging_from_arguments(arguments) == (None, None)


def test_paging_falls_back_to_top_level_bounds():
    assert paging_from_arguments({"request_data": {"other": 1}, "search_from": 5, "search_to": 9}) == (5, 9)
    assert paging_from_arguments({"request_data": {"search_from": "bad"}, "search_from": 7}) == (7, None)
    assert paging_from_arguments({"request_data": {"search_to": True}, "search_to": 9}) == (None, 9)
    # Valid request_data bounds still win over the top-level pair.
    assert paging_from_arguments(
        {"request_data": {"search_from": 1, "search_to": 2}, "search_from": 5, "search_to": 9}
    ) == (1, 2)


@pytest.mark.asyncio
async def test_middleware_uses_top_level_bounds_when_request_data_lacks_them():
    rows = [{"id": index} for index in range(30)]

    async def call_next(context):
        return ToolResult(structured_content={"reply": {"data": rows, "result_count": len(rows)}})

    result = await ResponseEnvelopeMiddleware().on_call_tool(
        _context({"request_data": {"other": 1}, "search_from": 0, "search_to": 1}),
        call_next,
    )
    parsed = json.loads(result.content[0].text)
    assert parsed["reply"]["data"] == [{"id": 0}]
    assert parsed["reply"]["result_count"] == 30
    assert parsed["returned"] == 1
    assert parsed["total"] == 30


def test_non_positive_max_bytes_disables_the_cap():
    # MCP_MAX_RESPONSE_BYTES is an unbounded int field (src/config/config.py:49),
    # so 0 / negative are reachable configurations that turn the cap off.
    rows = [{"id": index} for index in range(500)]
    for limit in (0, -1):
        enveloped = apply_response_envelope(rows, max_bytes=limit)
        assert enveloped["data"] == rows
        assert enveloped["total"] == 500
        assert "truncated" not in enveloped
        assert "returned" not in enveloped


def test_scalar_json_values_pass_through_untouched():
    assert apply_response_envelope(42) == 42
    assert apply_response_envelope(3.5) == 3.5
    assert apply_response_envelope(True) is True
    assert apply_response_envelope(None) is None


def test_scalar_json_text_block_keeps_text_and_no_structured_object():
    for text in ("42", "true", "null", "3.5"):
        result = envelope_tool_result(ToolResult(content=[TextContent(type="text", text=text)]))
        assert result.content[0].text == text
        assert result.structured_content is None
        assert result.is_error is False


def test_non_text_content_blocks_pass_through_untouched():
    image = ImageContent(type="image", data="aW1hZ2U=", mime_type="image/png")
    audio = AudioContent(type="audio", data="YXVkaW8=", mime_type="audio/wav")
    resource = EmbeddedResource(
        type="resource",
        resource=TextResourceContents(uri="file:///note.txt", text="hi", mime_type="text/plain"),
    )
    text = TextContent(type="text", text=json.dumps({"reply": {"observation_time": 10}}))

    result = envelope_tool_result(ToolResult(content=[text, image, audio, resource]))

    assert len(result.content) == 4
    # Identity proves the blocks are passed through byte-for-byte, not rebuilt.
    assert result.content[1] is image
    assert result.content[2] is audio
    assert result.content[3] is resource
    assert json.loads(result.content[0].text)["reply"]["observation_time"] == 10


def test_multi_block_success_false_escalates_to_error_without_structured_content():
    ok = TextContent(type="text", text=json.dumps({"success": "true", "reply": []}))
    bad = TextContent(type="text", text=json.dumps({"success": "false", "error": "nope"}))

    result = envelope_tool_result(ToolResult(content=[ok, bad]))
    assert result.is_error is True
    assert result.structured_content is None
    assert json.loads(result.content[1].text)["success"] == "false"

    first = TextContent(type="text", text=json.dumps({"success": "true", "reply": []}))
    second = TextContent(type="text", text=json.dumps({"success": "true", "reply": []}))
    both_ok = envelope_tool_result(ToolResult(content=[first, second]))
    assert both_ok.is_error is False
    assert both_ok.structured_content is None


def test_fit_count_with_zero_max_count_returns_zero():
    # Optional line-180 helper guard: a zero-row budget fits nothing.
    assert _fit_count(lambda count: {"rows": list(range(count))}, 0, 1000) == 0
