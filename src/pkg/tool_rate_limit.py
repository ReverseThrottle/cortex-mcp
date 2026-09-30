"""Limit tool invocations without counting tools/list or other requests."""

from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware


class ToolCallRateLimitMiddleware(RateLimitingMiddleware):
    """Token bucket applied only to ``tools/call``.

    The base class limits every request. Listing tools and health checks must
    not spend that budget. ``tools/call`` is the invocation the spec requires
    a server to limit.
    """

    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        return await RateLimitingMiddleware.on_request(self, context, call_next)
