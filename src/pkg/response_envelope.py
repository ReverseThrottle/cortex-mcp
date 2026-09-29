import json
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from entities.llm_config import LLM_FORMATTING_BASE_INSTRUCTIONS


def ensure_formatting_metadata(payload: dict) -> dict:
    """Add shared formatting instructions without replacing metadata a tool already set.

    Dict ``_metadata`` is copied and gains ``formatting_instructions`` only when
    that key is absent. Any other existing ``_metadata`` value stays as the tool
    set it.
    """
    if "_metadata" not in payload:
        payload["_metadata"] = {"formatting_instructions": LLM_FORMATTING_BASE_INSTRUCTIONS}
        return payload
    metadata = payload["_metadata"]
    if not isinstance(metadata, dict):
        return payload
    copied = dict(metadata)
    copied.setdefault("formatting_instructions", LLM_FORMATTING_BASE_INSTRUCTIONS)
    payload["_metadata"] = copied
    return payload


def _fastmcp_wrapped_array(value: dict) -> list | None:
    """Return the list when FastMCP stored a top-level JSON array as ``{"result": [...]}``.

    OpenAPI tools with no output schema wrap a non-object body before middleware
    runs. A list under ``result`` is that array. A Cortex object that has other
    fields, including its own ``result`` list, is left as an object.
    """
    if list(value) != ["result"]:
        return None
    body = value["result"]
    if isinstance(body, list):
        return body
    return None


def _fastmcp_wrapped_json_object(value: dict) -> dict | None:
    """Return the object when FastMCP stored a ``-> str`` tool as ``{"result": "<json>"}``.

    Handwritten tools return ``create_response`` text. FastMCP's string output
    schema stores that text under ``result`` and leaves the object fields inside
    the string. A non-object string stays wrapped.
    """
    if list(value) != ["result"]:
        return None
    body = value["result"]
    if not isinstance(body, str):
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def apply_response_envelope(value: Any) -> Any:
    """Attach formatting metadata without rewriting Cortex field values.

    JSON objects keep every existing field, including timestamps and ``success``.
    A top-level JSON array cannot carry ``_metadata``, so it becomes
    ``{"data", "total", "_metadata"}``. FastMCP already wraps that array as
    ``{"result": [...]}``; that object uses the same ``data`` / ``total``
    envelope. A handwritten ``-> str`` tool is stored as
    ``{"result": "<json string>"}``. When that string is a JSON object, its
    fields are the envelope, including ``reply``, ``success``, timestamps, and
    a formatting hint the tool already set. Other JSON values stay as they are.
    ``total`` and ``pagination`` are not added to objects, because Cortex
    endpoints do not share one pagination shape.
    """
    if isinstance(value, dict):
        wrapped = _fastmcp_wrapped_array(value)
        if wrapped is not None:
            return ensure_formatting_metadata({"data": wrapped, "total": len(wrapped)})
        parsed_object = _fastmcp_wrapped_json_object(value)
        if parsed_object is not None:
            return ensure_formatting_metadata(dict(parsed_object))
        return ensure_formatting_metadata(dict(value))
    if isinstance(value, list):
        return ensure_formatting_metadata({"data": value, "total": len(value)})
    return value


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _envelope_text(text: str) -> tuple[str, dict | None]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text, None
    enveloped = apply_response_envelope(parsed)
    if not isinstance(enveloped, dict):
        return text, None
    return _dump(enveloped), enveloped


def envelope_tool_result(result: ToolResult) -> ToolResult:
    """Apply the response envelope to the text the model reads and to structured content."""
    structured = result.structured_content
    enveloped_structured = apply_response_envelope(structured) if isinstance(structured, dict) else None

    new_content: list[Any] = []
    json_objects: list[dict] = []
    single_text = len(result.content) == 1 and isinstance(result.content[0], TextContent)
    for block in result.content:
        if isinstance(block, TextContent) and enveloped_structured is not None and single_text:
            new_content.append(TextContent(type="text", text=_dump(enveloped_structured)))
            json_objects.append(enveloped_structured)
            continue
        if isinstance(block, TextContent):
            text, obj = _envelope_text(block.text)
            new_content.append(TextContent(type="text", text=text))
            if obj is not None:
                json_objects.append(obj)
            continue
        new_content.append(block)

    structured_out = enveloped_structured
    if structured_out is None and len(json_objects) == 1 and len(new_content) == 1:
        structured_out = json_objects[0]

    return ToolResult(content=new_content, structured_content=structured_out, meta=result.meta)


class ResponseEnvelopeMiddleware(Middleware):
    """Add ``_metadata.formatting_instructions`` to JSON tool results.

    Registered after write confirmation so it sees the final result, including a refusal.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        result = await call_next(context)
        if not isinstance(result, ToolResult):
            return result
        return envelope_tool_result(result)
