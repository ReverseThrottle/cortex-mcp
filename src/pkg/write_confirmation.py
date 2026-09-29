import logging

from fastmcp.exceptions import NotFoundError
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel, Field

from config.config import get_config
from pkg.util import create_response

logger = logging.getLogger(__name__)

_CHANGES_STATE = "Side effects: this operation changes"
_CREATES_STATE = "Side effects: this operation creates"
_READ_ONLY = "Side effects: none"


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
    return ToolResult(content=create_response(data={"error": reason}, is_error=True))


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

        if not changes_tenant_state(tool.description):
            return await call_next(context)

        if ctx.request_context is None or not ctx.session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())
        ):
            return _refused(
                f"{tool_name} changes Cortex tenant state. MCP elicitation is enabled, "
                "but this client does not support it, so the operation was not run."
            )

        message = (
            f"{tool_name} changes Cortex tenant state. "
            "Set confirm to true to run it. Set confirm to false to leave the tenant unchanged."
        )
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
