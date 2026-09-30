import logging
from typing import Any

from fastmcp.exceptions import NotFoundError
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import InputRequiredToolResult, ToolResult
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from pydantic import BaseModel, Field

from config.config import get_config
from pkg.util import create_response

logger = logging.getLogger(__name__)

_CHANGES_STATE = "Side effects: this operation changes"
_CREATES_STATE = "Side effects: this operation creates"
_READ_ONLY = "Side effects: none"
_CONFIRM_ID = "confirm_write"


class WriteConfirmation(BaseModel):
    """Flat confirmation object. MCP elicitation allows only primitive fields."""

    confirm: bool = Field(
        description="True runs the operation and can change Cortex tenant state. False leaves the tenant unchanged."
    )


def changes_tenant_state(description: str | None) -> bool:
    """True when the tool description says the call writes, creates, or replaces tenant data."""
    text = description or ""
    if _READ_ONLY in text:
        return False
    return _CHANGES_STATE in text or _CREATES_STATE in text


def _refused(reason: str) -> ToolResult:
    return ToolResult(
        content=create_response(data={"error": reason}, is_error=True),
        is_error=True,
    )


def _is_modern_protocol(ctx: Any) -> bool:
    checker = getattr(ctx, "_is_modern_protocol", None)
    if not callable(checker):
        return False
    return bool(checker())


def _elicitation_supported(ctx: Any) -> bool | None:
    """True or false when the client advertised elicitation. None when that is unknown."""
    try:
        session = ctx.session
    except Exception:
        return None
    if session is None:
        return None
    try:
        return bool(session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability())))
    except Exception:
        return None


def _confirmation_message(tool_name: str) -> str:
    return (
        f"{tool_name} changes Cortex tenant state. "
        "Set confirm to true to run it. Set confirm to false to leave the tenant unchanged."
    )


def _confirmation_schema() -> dict:
    description = WriteConfirmation.model_fields["confirm"].description or ""
    return {
        "type": "object",
        "properties": {
            "confirm": {
                "type": "boolean",
                "description": description,
            }
        },
        "required": ["confirm"],
    }


def _ask_for_confirmation(tool_name: str) -> InputRequiredToolResult:
    """2026-07-28 has no server-initiated elicitation back-channel. Ask with SEP-2322."""
    return InputRequiredToolResult(
        InputRequiredResult(
            input_requests={
                _CONFIRM_ID: ElicitRequest(
                    params=ElicitRequestFormParams(
                        message=_confirmation_message(tool_name),
                        requested_schema=_confirmation_schema(),
                    )
                )
            }
        )
    )


def _modern_confirmation(responses: Any) -> bool | None:
    """True runs the tool, False refuses, None means the client has not answered yet."""
    if not isinstance(responses, dict) or _CONFIRM_ID not in responses:
        return None
    answer = responses[_CONFIRM_ID]
    if not isinstance(answer, ElicitResult) or answer.action != "accept":
        return False
    content = answer.content
    if not isinstance(content, dict):
        return False
    return bool(content.get("confirm"))


class WriteConfirmationMiddleware(Middleware):
    """Ask the MCP client to confirm a mutating tool before it runs.

    MCP_ELICITATION_ENABLED false leaves every call unchanged. The write and
    isolate flags still decide which tools are registered.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ):
        if not get_config().elicitation_enabled:
            return await call_next(context)

        ctx = context.fastmcp_context
        if ctx is None:
            return await call_next(context)

        tool_name = context.message.name
        try:
            tool = await ctx.fastmcp.get_tool(tool_name)
        except NotFoundError:
            return await call_next(context)

        if tool is None or not changes_tenant_state(tool.description):
            return await call_next(context)

        unsupported = (
            f"{tool_name} changes Cortex tenant state. MCP elicitation is enabled, "
            "but this client does not support it, so the operation was not run."
        )
        supported = _elicitation_supported(ctx)

        if _is_modern_protocol(ctx):
            if supported is False:
                return _refused(unsupported)
            decision = _modern_confirmation(getattr(ctx, "input_responses", None))
            if decision is None:
                return _ask_for_confirmation(tool_name)
            if not decision:
                return _refused(f"{tool_name} was not confirmed, so the operation was not run.")
            return await call_next(context)

        if ctx.request_context is None or not supported:
            return _refused(unsupported)

        message = _confirmation_message(tool_name)
        try:
            result = await ctx.elicit(message, WriteConfirmation)  # type: ignore[arg-type]
        except Exception:
            logger.exception("MCP elicitation failed for %s", tool_name)
            return _refused(f"Confirmation for {tool_name} could not be completed, so the operation was not run.")

        if isinstance(result, AcceptedElicitation) and bool(getattr(result.data, "confirm", False)):
            return await call_next(context)

        if isinstance(result, (DeclinedElicitation, CancelledElicitation, AcceptedElicitation)):
            return _refused(f"{tool_name} was not confirmed, so the operation was not run.")

        return _refused(f"Confirmation for {tool_name} was not accepted, so the operation was not run.")
