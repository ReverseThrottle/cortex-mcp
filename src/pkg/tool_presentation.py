"""Titles, annotations, and output schemas shared by handwritten and catalog tools."""

import inspect
import json
from collections.abc import Callable
from typing import Any

from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

_READ_ONLY = "Side effects: none"

LOOSE_OBJECT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}

# The tenant sorts get_instances by MODIFIED_AT when sort is omitted. That field
# is not a valid mapping (HTTP 500). FastMCP does not send schema defaults, so
# the tool call injects this documented field instead.
GET_CLOUD_INSTANCES_OPERATION = "post_cloud_onboarding_get_instances"
DEFAULT_CLOUD_INSTANCE_SORT: list[dict[str, str]] = [{"FIELD": "STATUS", "ORDER": "ASC"}]

_JSON_MEDIA_SUFFIXES = ("+json",)
_JSON_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/vnd.api+json",
        "application/hal+json",
        "application/ld+json",
        "text/json",
    }
)


def annotations_for_description(description: str | None) -> ToolAnnotations:
    """Read tools are not destructive. Every tool talks to a Cortex tenant or Broker VM."""
    read_only = _READ_ONLY in (description or "")
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=not read_only,
        idempotent_hint=read_only,
        open_world_hint=True,
    )


def _title_from_line(line: str) -> str | None:
    """Return the first sentence that describes the tool rather than its side effect."""
    if line.startswith("Side effects:"):
        if ". " not in line:
            return None
        line = line.split(". ", 1)[1].strip()
    if not line:
        return None
    lowered = line.lower()
    if lowered.startswith("this is a read-only") or lowered.startswith("confirm the"):
        return None
    return line.split(". ")[0].strip().rstrip(".")


def display_title(*, summary: str | None = None, description: str | None = None) -> str | None:
    """Use an OpenAPI summary, otherwise the first docstring sentence that is not a side-effect label."""
    if summary and summary.strip():
        return summary.strip()
    for raw_line in (description or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("args:") or lowered.startswith("returns:"):
            break
        title = _title_from_line(line)
        if title:
            return title
    return None


def publishable_output_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Publish an object schema the enveloped result can satisfy.

    A schema that forbids extra properties, or that wraps the body under
    ``result``, does not match the envelope the client receives. Those are
    replaced with an object schema that allows the envelope fields. An object
    schema that already allows extra properties is left as it is.
    """
    if not isinstance(schema, dict) or not schema:
        return dict(LOOSE_OBJECT_OUTPUT_SCHEMA)
    if schema.get("x-fastmcp-wrap-result"):
        return dict(LOOSE_OBJECT_OUTPUT_SCHEMA)
    if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
        return schema
    return dict(LOOSE_OBJECT_OUTPUT_SCHEMA)


def _as_structured_object(value: Any) -> Any:
    """Return a JSON object so a published object schema matches the tool body.

    Handwritten tools return ``create_response`` text. A JSON object becomes
    that object. Any other JSON value is stored under ``result`` so the
    response envelope can turn an array into ``data`` / ``total``.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"result": value}
        value = parsed
    if isinstance(value, dict):
        return value
    return {"result": value}


def _copy_tool_identity(wrapper: Callable, fn: Callable) -> None:
    """Keep the tool name and parameters, and advertise an object return value.

    A copied ``-> str`` annotation makes FastMCP serialize the object as a
    string. The published output schema is an object, so the wrapper's
    signature returns ``dict``.
    """
    annotations = dict(getattr(fn, "__annotations__", {}))
    annotations["return"] = dict
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__qualname__ = getattr(fn, "__qualname__", fn.__name__)
    wrapper.__module__ = getattr(fn, "__module__", wrapper.__module__)
    wrapper.__annotations__ = annotations
    wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=dict)  # type: ignore[attr-defined]


def return_structured_object(fn: Callable) -> Callable:
    """Wrap a tool function so its return value is a JSON object."""

    if _is_coroutine(fn):

        async def async_wrapped(*args, **kwargs):
            return _as_structured_object(await fn(*args, **kwargs))

        _copy_tool_identity(async_wrapped, fn)
        return async_wrapped

    def wrapped(*args, **kwargs):
        return _as_structured_object(fn(*args, **kwargs))

    _copy_tool_identity(wrapped, fn)
    return wrapped


def _is_coroutine(fn: Callable) -> bool:
    return inspect.iscoroutinefunction(fn)


def _json_media_type(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    return base in _JSON_MEDIA_TYPES or base.endswith(_JSON_MEDIA_SUFFIXES)


def _returns_unstructured_body(route: Any) -> bool:
    """Download and non-JSON operations do not produce an object body.

    ``publishable_output_schema`` would still advertise an object. An empty or
    plain-text body then has no ``structuredContent``, and the MCP client
    rejects the result. Those tools publish no output schema.
    """
    operation_id = getattr(route, "operation_id", None) or ""
    if "_download" in operation_id:
        return True
    responses = getattr(route, "responses", None) or {}
    saw_content = False
    for code, response in responses.items():
        if not (str(code).startswith("2") or str(code) in {"default", "2xx"}):
            continue
        content = getattr(response, "content_schema", None) or {}
        if not content:
            continue
        saw_content = True
        if any(_json_media_type(media_type) for media_type in content):
            return False
    return saw_content


def with_default_cloud_instance_sort(arguments: dict[str, Any]) -> dict[str, Any]:
    """Add the documented STATUS sort when the caller did not send one.

    An empty ``request_data`` is the default tool call. The tenant then sorts
    by MODIFIED_AT and returns HTTP 500. A caller-supplied sort is kept.
    """
    request_data = arguments.get("request_data")
    if request_data is None:
        request_data = {}
    if not isinstance(request_data, dict):
        return arguments
    filter_data = request_data.get("filter_data")
    if filter_data is None:
        filter_data = {}
    if not isinstance(filter_data, dict):
        return arguments
    sort = filter_data.get("sort")
    if isinstance(sort, list) and sort:
        return arguments
    updated_filter = dict(filter_data)
    updated_filter["sort"] = [dict(item) for item in DEFAULT_CLOUD_INSTANCE_SORT]
    updated_request = dict(request_data)
    updated_request["filter_data"] = updated_filter
    updated = dict(arguments)
    updated["request_data"] = updated_request
    return updated


def _install_default_cloud_instance_sort(component: Any) -> None:
    original_run = component.run

    async def run_with_default_sort(arguments: dict[str, Any]) -> Any:
        return await original_run(with_default_cloud_instance_sort(arguments))

    # OpenAPITool is a Pydantic model. Assigning run through the model rejects
    # the method because it is not a field. The instance attribute still
    # shadows OpenAPITool.run when the server calls it.
    object.__setattr__(component, "run", run_with_default_sort)


def annotate_openapi_component(route: Any, component: Any) -> None:
    """Apply titles, side-effect annotations, and a matching output schema."""
    if not isinstance(component, Tool):
        return
    description = component.description or getattr(route, "description", None)
    component.annotations = annotations_for_description(description)
    title = display_title(summary=getattr(route, "summary", None), description=description)
    if title:
        component.title = title
    if _returns_unstructured_body(route):
        component.output_schema = None
    else:
        component.output_schema = publishable_output_schema(component.output_schema)
    if getattr(route, "operation_id", None) == GET_CLOUD_INSTANCES_OPERATION:
        _install_default_cloud_instance_sort(component)
