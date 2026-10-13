"""Bound one tools/call so a slow Cortex request returns before the MCP client times out."""

import asyncio
import contextvars
import json
import time
from typing import Any
from urllib.parse import urlsplit

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from config.config import get_config
from entities.exceptions import PAPIDeadlineError
from pkg.util import create_response

# A shorter attempt than this cannot produce a useful response. Stop and
# report the deadline instead of starting it.
_MIN_ATTEMPT_SECONDS = 0.05
_PUBLIC_API_PREFIX = "/public_api/v1/"
_TIMEOUT_MARKER = "Cortex API did not respond within"

# Absolute monotonic deadline for the tool call currently running in this task.
_call_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("cortex_call_deadline", default=None)
# The configured budget, in seconds, that produced that deadline.
_call_budget: contextvars.ContextVar[float | None] = contextvars.ContextVar("cortex_call_budget", default=None)

_DeadlineTokens = tuple[contextvars.Token[float | None], contextvars.Token[float | None]]


def format_seconds(seconds: float) -> str:
    """Render a timeout without a trailing decimal on whole seconds."""
    rounded = round(float(seconds), 3)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def endpoint_label(url: str) -> str:
    """Return the Cortex path without the public API prefix or the tenant host."""
    path = urlsplit(url).path or url
    index = path.lower().find(_PUBLIC_API_PREFIX)
    if index >= 0:
        path = path[index + len(_PUBLIC_API_PREFIX) :]
    label = path.strip("/")
    return label or url


def upstream_timeout_message(url: str, seconds: float) -> str:
    """Tool-error text for a Cortex call that did not answer in time."""
    return f"{_TIMEOUT_MARKER} {format_seconds(seconds)} s for {endpoint_label(url)}"


def timeout_detail(exc: BaseException) -> str | None:
    """Return the clear timeout sentence when an error already carries one."""
    text = str(exc)
    index = text.find(_TIMEOUT_MARKER)
    if index < 0:
        return None
    return text[index:].strip()


def is_slow_report_or_search(url: str) -> bool:
    """Report and search reads are the calls that stall until the client gives up.

    A read timeout on these paths is not retried. Connection failures and
    HTTP 429/503 still follow the normal retry policy, inside the deadline.
    """
    path = urlsplit(url).path.lower().rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if "search" in segments:
        return True
    return any("report" in segment for segment in segments)


def arm_call_deadline(seconds: float) -> _DeadlineTokens:
    """Start the wall-clock budget for the current task."""
    deadline_token = _call_deadline.set(time.monotonic() + seconds)
    budget_token = _call_budget.set(seconds)
    return deadline_token, budget_token


def reset_call_deadline(tokens: _DeadlineTokens) -> None:
    deadline_token, budget_token = tokens
    _call_deadline.reset(deadline_token)
    _call_budget.reset(budget_token)


def call_deadline_is_set() -> bool:
    return _call_deadline.get() is not None


def remaining_seconds() -> float | None:
    deadline = _call_deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def armed_budget_seconds() -> float:
    budget = _call_budget.get()
    if budget is None:
        return 0.0
    return budget


def deadline_exhausted() -> bool:
    remaining = remaining_seconds()
    if remaining is None:
        return False
    return remaining < _MIN_ATTEMPT_SECONDS


def _tool_name(context: MiddlewareContext) -> str:
    message = getattr(context, "message", None)
    name = getattr(message, "name", None)
    if isinstance(name, str) and name:
        return name
    return "Cortex API"


def _error_result(message: str) -> ToolResult:
    body = json.loads(create_response(data={"error": message}, is_error=True))
    return ToolResult(content=body, structured_content=body, is_error=True)


class ToolCallDeadlineMiddleware(Middleware):
    """Return an isError tool result when a tools/call outlives its deadline.

    A hung Cortex call never reaches the client: the MCP client times out
    first and the caller only sees JSON-RPC -32001. This middleware is the
    backstop. The HTTP client uses the same budget for each attempt and retry,
    and a timeout it already reported is returned as that same sentence.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        seconds = float(get_config().cortex_call_deadline_s)
        tokens = arm_call_deadline(seconds)
        try:
            try:
                return await asyncio.wait_for(call_next(context), timeout=seconds)
            except TimeoutError:
                return _error_result(upstream_timeout_message(_tool_name(context), seconds))
            except (PAPIDeadlineError, ToolError) as exc:
                detail = timeout_detail(exc)
                if detail is None:
                    if isinstance(exc, ToolError):
                        raise
                    detail = upstream_timeout_message(_tool_name(context), seconds)
                return _error_result(detail)
        finally:
            reset_call_deadline(tokens)
