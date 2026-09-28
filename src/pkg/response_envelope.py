import json
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from entities.llm_config import LLM_FORMATTING_BASE_INSTRUCTIONS


def ensure_formatting_metadata(payload: dict) -> dict:
    """Add shared formatting instructions without replacing metadata a tool already set."""
    metadata = payload.get("_metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
    else:
        metadata = {}
    metadata.setdefault("formatting_instructions", LLM_FORMATTING_BASE_INSTRUCTIONS)
    payload["_metadata"] = metadata
    return payload


def apply_response_envelope(value: Any) -> Any:
    """Attach formatting metadata without rewriting Cortex field values.

    JSON objects keep every existing field, including timestamps and ``success``.
    A JSON array cannot carry ``_metadata``, so it is wrapped as
    ``{"data", "total", "_metadata"}``. Other JSON values stay as they are.
    ``total`` and ``pagination`` are not invented for objects: Cortex endpoints
    do not share one pagination shape.
    """
    if isinstance(value, dict):
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
