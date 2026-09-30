"""Limit tool invocations without counting tools/list or other requests."""

import json

from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import (
    RateLimitError,
    RateLimitingMiddleware,
)
from fastmcp.tools import ToolResult

from pkg.util import create_response


class ToolCallRateLimitMiddleware(RateLimitingMiddleware):
    """Token bucket applied only to ``tools/call``.

    The base class limits every request. Listing tools and health checks must
    not spend that budget. An excess ``tools/call`` is an ``isError`` tool
    result that uses the JSON envelope. It is not a JSON-RPC protocol error.
    """

    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        try:
            return await RateLimitingMiddleware.on_request(self, context, call_next)
        except RateLimitError as exc:
            body = json.loads(create_response(data={"error": exc.message}, is_error=True))
            return ToolResult(content=body, structured_content=body, is_error=True)
