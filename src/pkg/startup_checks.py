"""Startup consistency checks for the safety settings.

A setting that can never be satisfied on the running transport must be loud at
startup, not a silent refusal of every tool call.
"""

import logging

from config.config import Settings
from pkg.tool_classification import (
    HANDWRITTEN_SIDE_EFFECTS,
    SideEffect,
    catalog_classifications,
    catalog_operation_ids,
)

logger = logging.getLogger("Cortex MCP")

_STATELESS_TRANSPORT = "streamable-http"


def check_write_confirmation(config: Settings, transport: str) -> None:
    """Reject or flag a confirmation mode that the transport cannot honor.

    ``transport`` is the resolved FastMCP transport name. The HTTP handler is
    stateless, so legacy elicitation has no session and no back-channel there.
    """
    mode = config.effective_write_confirmation
    if "elicitation_enabled" in config.model_fields_set:
        logger.warning(
            "MCP_ELICITATION_ENABLED is deprecated. Set MCP_WRITE_CONFIRMATION to client, elicit, or require. "
            "Current resolved mode: %s.",
            mode,
        )
    if transport != _STATELESS_TRANSPORT or mode == "client":
        return
    if mode == "require":
        if config.write_confirmation is None and config.elicitation_enabled:
            raise ValueError(
                "MCP_ELICITATION_ENABLED=true is deprecated and now resolves to MCP_WRITE_CONFIRMATION=require, "
                "which cannot be satisfied over streamable-http: the HTTP handler is stateless, so every write "
                "would be refused. Set MCP_WRITE_CONFIRMATION=client (clients approve from the tool annotations) "
                "or MCP_WRITE_CONFIRMATION=elicit, remove MCP_ELICITATION_ENABLED, or run over stdio."
            )
        raise ValueError(
            "MCP_WRITE_CONFIRMATION=require cannot be satisfied over streamable-http: the HTTP handler is "
            "stateless, so every write would be refused. Set MCP_WRITE_CONFIRMATION=client (clients approve "
            "from the tool annotations) or run over stdio."
        )
    logger.warning(
        "MCP_WRITE_CONFIRMATION=elicit cannot be satisfied over streamable-http on the legacy protocol: "
        "writes will run without a server prompt unless the client uses the 2026-07-28 protocol."
    )


def known_tool_classes() -> dict[str, SideEffect | None]:
    """Every tool name the server can register, mapped to its side-effect class.

    A catalog operation with no class maps to ``None``. It can be registered, so
    DENY may name it. ALLOW rejects it because ``None`` is not write or action.
    """
    known: dict[str, SideEffect | None] = dict.fromkeys(catalog_operation_ids())
    known.update(catalog_classifications())
    known.update(HANDWRITTEN_SIDE_EFFECTS)
    return known


_ISOLATE_TOOLS = frozenset({"isolate_endpoint", "unisolate_endpoint"})


def check_tool_controls(config: Settings) -> None:
    """Fail startup on unknown names or an allow list that names the wrong class."""
    known = known_tool_classes()
    named = config.tools_allow_set | config.tools_deny_set
    unknown = sorted(named - known.keys())
    if unknown:
        raise ValueError(f"MCP_TOOLS_ALLOW or MCP_TOOLS_DENY names unknown tools: {', '.join(unknown)}")
    wrong = sorted(
        name for name in config.tools_allow_set if known[name] not in ("write", "action") or name in _ISOLATE_TOOLS
    )
    if wrong:
        raise ValueError(
            "MCP_TOOLS_ALLOW can only enable write or action tools. It cannot enable read, secret, or "
            f"isolate tools: {', '.join(wrong)}"
        )
