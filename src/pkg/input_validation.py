"""JSON Schema checks for tool arguments, in addition to handwritten Pydantic models."""

import copy
import logging
from typing import Any

import jsonschema
from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from jsonschema.exceptions import SchemaError, ValidationError

from pkg.util import create_response

logger = logging.getLogger(__name__)


def apply_required_defaults(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Fill missing required properties that publish a JSON Schema default.

    OpenAPI tools copy arguments into the HTTP body and do not apply defaults.
    A required ``request_data`` with ``default: {}`` is how an empty categories
    call becomes ``{"request_data": {}}``. Optional defaults stay omitted so
    other requests keep the shape they already send.
    """
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return arguments
    for name in required:
        if not isinstance(name, str) or name in arguments:
            continue
        prop = properties.get(name)
        if isinstance(prop, dict) and "default" in prop:
            arguments[name] = copy.deepcopy(prop["default"])
    return arguments


class JsonSchemaInputMiddleware(Middleware):
    """Reject tool arguments that do not match the tool's published input schema.

    Function tools still run FastMCP's Pydantic validation after this check.
    A schema the library cannot compile does not block the call.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        ctx = context.fastmcp_context
        if ctx is None:
            return await call_next(context)

        tool_name = context.message.name
        try:
            tool = await ctx.fastmcp.get_tool(tool_name)
        except NotFoundError:
            return await call_next(context)

        if tool is None:
            return await call_next(context)

        schema = tool.parameters
        if not isinstance(schema, dict) or not schema:
            return await call_next(context)

        arguments = dict(context.message.arguments or {})
        apply_required_defaults(arguments, schema)
        context.message.arguments = arguments
        try:
            jsonschema.validate(instance=arguments, schema=schema)
        except SchemaError:
            logger.warning("Skipping JSON Schema check for %s; the input schema is not usable", tool_name)
            return await call_next(context)
        except ValidationError as exc:
            return ToolResult(
                content=create_response(
                    data={"error": f"Invalid arguments for {tool_name}: {exc.message}"},
                    is_error=True,
                ),
                is_error=True,
            )

        return await call_next(context)
