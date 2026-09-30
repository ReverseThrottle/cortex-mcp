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


def annotate_openapi_component(route, component) -> None:
    """Apply titles, side-effect annotations, and a matching output schema."""
    if not isinstance(component, Tool):
        return
    description = component.description or getattr(route, "description", None)
    component.annotations = annotations_for_description(description)
    title = display_title(summary=getattr(route, "summary", None), description=description)
    if title:
        component.title = title
    component.output_schema = publishable_output_schema(component.output_schema)
