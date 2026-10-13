import json
from collections.abc import Callable
from typing import Any, Literal, NamedTuple, assert_never

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import InputRequiredToolResult, ToolResult
from mcp.types import TextContent

from entities.llm_config import LLM_FORMATTING_BASE_INSTRUCTIONS

# compliance/get_controls documents search_to default 100 and then ignores it.
DEFAULT_RESPONSE_PAGE_SIZE = 100
# A full controls page is several megabytes. Keep the model payload near 256 KB.
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024

_RowKind = Literal["reply_data", "reply_list", "reply_field", "data"]


def inline_formatting_instructions_enabled() -> bool:
    """True when a client asked to keep formatting text inside each tool result.

    Imported lazily: ``config`` imports ``pkg.util``, which imports this module.
    """
    from config.config import get_config

    return get_config().inline_formatting_instructions


def ensure_formatting_metadata(payload: dict) -> dict:
    """Add shared formatting instructions only when that opt-in is enabled.

    The default is a no-op. Clients receive ``LLM_FORMATTING_BASE_INSTRUCTIONS``
    once, as the server ``instructions`` sent at initialize. Set
    ``MCP_INLINE_FORMATTING_INSTRUCTIONS=true`` to restore
    ``_metadata.formatting_instructions`` on each result. Dict ``_metadata`` is
    copied and gains ``formatting_instructions`` only when that key is absent.
    Any other existing ``_metadata`` value stays as the tool set it.
    """
    if not inline_formatting_instructions_enabled():
        return payload
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


class _Rows(NamedTuple):
    kind: _RowKind
    rows: list
    field: str | None = None


def _bounds(search_from: int | None, search_to: int | None) -> tuple[int, int]:
    start = 0 if search_from is None else max(search_from, 0)
    if search_to is None:
        end = start + DEFAULT_RESPONSE_PAGE_SIZE
    else:
        end = search_to
    if end < start:
        end = start
    return start, end


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def paging_from_arguments(arguments: Any) -> tuple[int | None, int | None]:
    """Read ``search_from`` / ``search_to`` from a tool call.

    Catalog tools nest them under ``request_data``. A top-level pair is used
    when the body has no ``request_data`` object.
    """
    if not isinstance(arguments, dict):
        return None, None
    request_data = arguments.get("request_data")
    source = request_data if isinstance(request_data, dict) else arguments
    search_from = _optional_int(source.get("search_from")) if "search_from" in source else None
    search_to = _optional_int(source.get("search_to")) if "search_to" in source else None
    if isinstance(request_data, dict):
        if search_from is None and "search_from" in arguments:
            search_from = _optional_int(arguments.get("search_from"))
        if search_to is None and "search_to" in arguments:
            search_to = _optional_int(arguments.get("search_to"))
    return search_from, search_to


def _encoded_size(value: Any) -> int:
    return len(_dump(value).encode("utf-8"))


def _locate_rows(payload: dict) -> _Rows | None:
    reply = payload.get("reply")
    if isinstance(reply, dict):
        data = reply.get("data")
        if isinstance(data, list):
            return _Rows("reply_data", data)
        list_fields = [key for key, item in reply.items() if isinstance(item, list)]
        if len(list_fields) == 1:
            field = list_fields[0]
            return _Rows("reply_field", reply[field], field)
    if isinstance(reply, list):
        return _Rows("reply_list", reply)
    data = payload.get("data")
    allowed = {"data", "total", "_metadata", "truncated", "returned"}
    if isinstance(data, list) and set(payload) <= allowed:
        return _Rows("data", data)
    return None


def _assign_rows(payload: dict, located: _Rows, rows: list) -> dict:
    copied = dict(payload)
    kind = located.kind
    if kind == "reply_data":
        reply = dict(copied["reply"])
        reply["data"] = rows
        copied["reply"] = reply
    elif kind == "reply_list":
        copied["reply"] = rows
    elif kind == "reply_field":
        field = located.field
        if field is None:
            return copied
        reply = dict(copied["reply"])
        reply[field] = rows
        copied["reply"] = reply
    elif kind == "data":
        copied["data"] = rows
    else:
        assert_never(kind)
    return copied


def _fit_count(make: Callable[[int], Any], max_count: int, max_bytes: int) -> int:
    if max_count <= 0:
        return 0
    if _encoded_size(make(max_count)) <= max_bytes:
        return max_count
    best = 0
    low = 0
    high = max_count - 1
    while low <= high:
        mid = (low + high) // 2
        if _encoded_size(make(mid)) <= max_bytes:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _mark_truncated(payload: dict, returned: int, total: int) -> dict:
    payload["truncated"] = True
    payload["returned"] = returned
    payload["total"] = total
    return payload


def _page_window(located: _Rows, search_from: int | None, search_to: int | None) -> list:
    """Rows to keep from a located list.

    ``reply.data``, a list ``reply``, and a top-level array use the requested
    window or a default page of 100. A named list inside ``reply`` is sliced
    only when it is longer than that window. An array no longer than the
    window is the page Cortex already returned, including when ``search_from``
    is past zero, so it is not indexed again.
    """
    rows = located.rows
    explicit = search_from is not None or search_to is not None
    if located.kind == "reply_field" and not explicit:
        return rows
    start, end = _bounds(search_from, search_to)
    if len(rows) <= end - start:
        return rows
    return rows[start:end]


def _limit_rows(
    enveloped: dict, located: _Rows, search_from: int | None, search_to: int | None, max_bytes: int
) -> dict:
    rows = located.rows
    total = len(rows)
    window = _page_window(located, search_from, search_to)

    def assemble(count: int, *, mark: bool) -> dict:
        payload = _assign_rows(enveloped, located, window[:count])
        if mark:
            return _mark_truncated(payload, count, total)
        return payload

    if len(window) == total and _encoded_size(enveloped) <= max_bytes:
        return enveloped
    shortened = len(window) != total
    if _encoded_size(assemble(len(window), mark=shortened)) <= max_bytes:
        if not shortened:
            return enveloped
        return assemble(len(window), mark=True)
    count = _fit_count(lambda size: assemble(size, mark=True), len(window), max_bytes)
    return assemble(count, mark=True)


def _cap_without_rows(payload: dict, max_bytes: int) -> dict:
    if _encoded_size(payload) <= max_bytes:
        return payload
    raw = _dump(payload).encode("utf-8")

    def preview(size: int) -> dict:
        body = {
            "truncated": True,
            "returned": 0,
            "preview": raw[:size].decode("utf-8", errors="ignore"),
        }
        return ensure_formatting_metadata(body)

    return preview(_fit_count(preview, len(raw), max_bytes))


def _limit_enveloped(
    enveloped: Any,
    search_from: int | None,
    search_to: int | None,
    max_bytes: int,
) -> Any:
    if not isinstance(enveloped, dict) or max_bytes <= 0:
        return enveloped
    located = _locate_rows(enveloped)
    if located is None:
        return _cap_without_rows(enveloped, max_bytes)
    return _limit_rows(enveloped, located, search_from, search_to, max_bytes)


def apply_response_envelope(
    value: Any,
    *,
    search_from: int | None = None,
    search_to: int | None = None,
    max_bytes: int | None = None,
) -> Any:
    """Attach formatting metadata without rewriting Cortex field values.

    JSON objects keep every existing field, including timestamps and ``success``.
    A top-level JSON array cannot carry ``_metadata``, so it becomes
    ``{"data", "total", "_metadata"}``. FastMCP already wraps that array as
    ``{"result": [...]}``; that object uses the same ``data`` / ``total``
    envelope. A handwritten ``-> str`` tool is stored as
    ``{"result": "<json string>"}``. When that string is a JSON object, its
    fields are the envelope, including ``reply``, ``success``, and timestamps.
    ``_metadata.formatting_instructions`` is added only when
    ``MCP_INLINE_FORMATTING_INSTRUCTIONS`` is set. Other JSON values stay as
    they are.
    ``total`` and ``pagination`` are not added to objects, because Cortex
    endpoints do not share one pagination shape. A plain string, including an
    empty download body, cannot satisfy an object output schema, so it is
    returned as ``{"content": "<text>"}``.

    A list under ``reply.data``, a list ``reply``, or a top-level array is
    sliced to ``search_from:search_to``. Missing bounds use a page of
    ``DEFAULT_RESPONSE_PAGE_SIZE``. A named list inside ``reply`` is sliced
    only when it is longer than that window. An array no longer than the
    window is the page Cortex already returned and is left intact. The kept
    rows are shortened again until the JSON fits in ``max_bytes``. A shortened
    payload gains ``truncated``, ``returned``, and ``total``.
    """
    limit = DEFAULT_MAX_RESPONSE_BYTES if max_bytes is None else max_bytes

    def finish(enveloped: Any) -> Any:
        return _limit_enveloped(enveloped, search_from, search_to, limit)

    if isinstance(value, dict):
        wrapped = _fastmcp_wrapped_array(value)
        if wrapped is not None:
            return finish(ensure_formatting_metadata({"data": wrapped, "total": len(wrapped)}))
        parsed_object = _fastmcp_wrapped_json_object(value)
        if parsed_object is not None:
            return finish(ensure_formatting_metadata(dict(parsed_object)))
        return finish(ensure_formatting_metadata(dict(value)))
    if isinstance(value, list):
        return finish(ensure_formatting_metadata({"data": value, "total": len(value)}))
    if isinstance(value, str):
        return finish(ensure_formatting_metadata({"content": value}))
    return value


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _envelope_text(
    text: str,
    *,
    search_from: int | None,
    search_to: int | None,
    max_bytes: int | None,
) -> tuple[str, dict | None]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    enveloped = apply_response_envelope(parsed, search_from=search_from, search_to=search_to, max_bytes=max_bytes)
    if not isinstance(enveloped, dict):
        return text, None
    return _dump(enveloped), enveloped


def _envelope_failed(payload: dict | None) -> bool:
    return isinstance(payload, dict) and payload.get("success") == "false"


def envelope_tool_result(
    result: ToolResult,
    *,
    search_from: int | None = None,
    search_to: int | None = None,
    max_bytes: int | None = None,
) -> ToolResult:
    """Apply the response envelope to the text the model reads and to structured content.

    A JSON envelope with ``success`` ``"false"`` is a tool error. Write-confirmation
    refusals and handwritten failures use that field and are returned with
    ``isError`` true. The JSON body stays the same envelope.
    """
    structured = result.structured_content
    enveloped_structured = (
        apply_response_envelope(structured, search_from=search_from, search_to=search_to, max_bytes=max_bytes)
        if isinstance(structured, dict)
        else None
    )

    new_content: list[Any] = []
    json_objects: list[dict] = []
    single_text = len(result.content) == 1 and isinstance(result.content[0], TextContent)
    for block in result.content:
        if isinstance(block, TextContent) and enveloped_structured is not None and single_text:
            new_content.append(TextContent(type="text", text=_dump(enveloped_structured)))
            json_objects.append(enveloped_structured)
            continue
        if isinstance(block, TextContent):
            text, obj = _envelope_text(block.text, search_from=search_from, search_to=search_to, max_bytes=max_bytes)
            new_content.append(TextContent(type="text", text=text))
            if obj is not None:
                json_objects.append(obj)
            continue
        new_content.append(block)

    structured_out = enveloped_structured
    if structured_out is None and len(json_objects) == 1 and len(new_content) == 1:
        structured_out = json_objects[0]

    is_error = result.is_error or _envelope_failed(structured_out)
    if not is_error and any(_envelope_failed(obj) for obj in json_objects):
        is_error = True

    return ToolResult(
        content=new_content,
        structured_content=structured_out,
        meta=result.meta,
        is_error=is_error,
    )


class ResponseEnvelopeMiddleware(Middleware):
    """Shape JSON tool results without embedding model instructions.

    Registered before write confirmation. FastMCP runs that first middleware
    first, so this one sees the final result, including a refusal. Formatting
    text is omitted unless ``MCP_INLINE_FORMATTING_INSTRUCTIONS`` is set.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        result = await call_next(context)
        if isinstance(result, InputRequiredToolResult):
            return result
        if not isinstance(result, ToolResult):
            return result
        # config imports pkg.util, and pkg.util imports this module.
        from config.config import get_config

        search_from, search_to = paging_from_arguments(getattr(context.message, "arguments", None))
        return envelope_tool_result(
            result,
            search_from=search_from,
            search_to=search_to,
            max_bytes=get_config().mcp_max_response_bytes,
        )
