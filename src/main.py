"""
Cortex MCP Server Main Module

This module serves as the entry point for the Cortex MCP (Model Context Protocol) Server.
It handles server initialization, signal handling for graceful shutdown, and manages
the async event loop for the MCP server operations.

The server can operate in different transport modes (stdio, streamable-http) and integrates
with XSIAM (Extended Security Intelligence and Automation Management) services.
"""

import asyncio
import logging
import signal
from functools import partial

from fastmcp import FastMCP
from fastmcp.server.providers.openapi.routing import MCPType
from starlette.middleware import Middleware

from config.config import get_config
from pkg.client import PAPIClient
from pkg.input_validation import JsonSchemaInputMiddleware
from pkg.portkey_session import PortkeySessionAdapter
from pkg.protocol_header import ModernProtocolHeaderMiddleware
from pkg.response_envelope import ResponseEnvelopeMiddleware
from pkg.setup_logging import setup_logging
from pkg.tool_presentation import annotate_openapi_component
from pkg.tool_rate_limit import ToolCallRateLimitMiddleware
from pkg.util import bundle_openapi_from_folders, get_papi_url
from pkg.write_confirmation import WriteConfirmationMiddleware
from service.cortex_mcp.server import create_mcp_server
from usecase.module_util import discover_and_register_modules
from version import __version__

logger = logging.getLogger("Cortex MCP")

mcp = FastMCP()


async def shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
    """
    Handle graceful shutdown of the Cortex MCP Server.

    This function is called when the server receives termination signals (SIGINT/SIGTERM).
    It cancels all running tasks and stops the event loop cleanly.

    Args:
        sig (signal.Signals): The signal that triggered the shutdown (SIGINT or SIGTERM)
        loop (asyncio.AbstractEventLoop): The current asyncio event loop to be stopped

    Returns:
        None
    """
    logger.info(f"Received exit signal {sig.name}...")

    # Get all running tasks except the current shutdown task
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]

    logger.info("Cancelling outstanding tasks")
    # Wait for all tasks to complete or be canceled
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("Stopping the event loop")
    loop.stop()


def csv_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def streamable_http_middleware(path: str, auth_token: str = "") -> list[Middleware]:
    """Middleware in front of the stateless Streamable HTTP handler.

    The adapter is outside the protocol-header check. Host and origin
    protection is installed by FastMCP further out, so a rejected host does
    not reach the adapter. The handler itself stays ``stateless_http``.
    """
    return [
        Middleware(PortkeySessionAdapter, path=path, auth_token=auth_token),
        Middleware(ModernProtocolHeaderMiddleware),
    ]


def resolve_transport(transport: str) -> str:
    """Return the FastMCP transport name.

    ``http`` is the Streamable HTTP alias. ``sse`` is the retired HTTP+SSE
    transport and is rejected before the server starts.
    """
    if transport == "stdio":
        return "stdio"
    if transport in {"streamable-http", "http"}:
        return "streamable-http"
    if transport == "sse":
        raise ValueError(
            "MCP_TRANSPORT=sse is not supported. The deprecated HTTP+SSE transport is not offered. "
            "Use stdio or streamable-http."
        )
    raise ValueError(f"MCP_TRANSPORT={transport!r} is not supported. Use stdio or streamable-http.")


async def async_main(transport: str):
    """
    Main async function that initializes and runs the Cortex MCP Server.

    This function sets up logging, configures signal handlers for graceful shutdown,
    creates the MCP server with authentication, imports the XSIAM server module,
    and starts the server with the appropriate transport configuration.

    Args:
        transport (str): The transport mechanism for the MCP server
                              (e.g., 'stdio', 'streamable-http')

    Returns:
        None

    Raises:
        Exception: Any exception that occurs during server initialization or runtime.
    """
    config = get_config()
    setup_logging(config)
    logger.info("Starting Cortex MCP Server")

    loop = asyncio.get_running_loop()

    # Add signal handlers for SIGINT and SIGTERM for graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(lambda s: asyncio.create_task(shutdown(s, loop)), sig))

    # Retrieve API credentials from environment variables
    api_key = config.papi_auth_header_key
    api_key_id = config.papi_auth_id_key
    papi_url = config.papi_url_env_key
    auth_token = config.mcp_auth_token

    if transport != "stdio" and not auth_token:
        logger.warning(
            "MCP_AUTH_TOKEN is not set — this server is reachable over the network " "with no client authentication."
        )

    mcp = await initialize_mcp_server(api_key, api_key_id, papi_url, auth_token)
    selected = resolve_transport(transport)

    # Start server with appropriate transport configuration
    if selected == "stdio":
        await mcp.run_async(transport="stdio")
        return

    # Stateless Streamable HTTP: the handler mints no session id and opens no
    # standalone GET SSE stream. PortkeySessionAdapter, in front of that
    # endpoint, stores Mcp-Session-Id for Portkey. Hosts default to loopback
    # so an Origin that matches a foreign Host is not treated as same-origin.
    # The Docker image widens MCP_ALLOWED_HOSTS.
    await mcp.run_http_async(
        transport="streamable-http",
        host=config.mcp_host,
        port=config.mcp_port,
        path=config.mcp_path,
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=csv_list(config.mcp_allowed_hosts),
        allowed_origins=csv_list(config.mcp_allowed_origins),
        middleware=streamable_http_middleware(config.mcp_path, auth_token),
    )


def openapi_route_map(route, mcp_type):
    """Hide mutating catalog tools unless MCP_WRITE_TOOLS_ENABLED is set."""
    if get_config().write_tools_enabled:
        return None
    description = getattr(route, "description", "") or ""
    if "Side effects: this operation changes" in description:
        return MCPType.EXCLUDE
    return None


async def initialize_mcp_server(api_key: str, api_key_id: str, papi_url: str, auth_token: str = "") -> FastMCP:
    # Create MCP server instance with authentication
    mcp = create_mcp_server(api_key, api_key_id, auth_token)
    config = get_config()
    # FastMCP runs the first middleware added first. The rate limit returns an
    # isError tool result with the JSON envelope and does not count tools/list.
    # The envelope is outside validation and write confirmation so a refusal or
    # a schema failure is enveloped the same way as a tool result.
    mcp.add_middleware(
        ToolCallRateLimitMiddleware(
            max_requests_per_second=config.tool_calls_per_second,
            burst_capacity=config.tool_call_burst,
            global_limit=True,
        )
    )
    mcp.add_middleware(ResponseEnvelopeMiddleware())
    mcp.add_middleware(JsonSchemaInputMiddleware())
    mcp.add_middleware(WriteConfirmationMiddleware())

    # Discover mcp components from modules
    discover_and_register_modules(mcp)

    # Discover mcp components from openapi specs and import them
    spec = bundle_openapi_from_folders()
    # Catalog calls include scans, exports, and XQL streams that outlive the 30s default.
    open_api_mcp = FastMCP.from_openapi(
        spec,
        PAPIClient(
            get_papi_url(papi_url),
            api_key,
            api_key_id,
            key_type=get_config().papi_key_type,
            timeout=300,
        ),
        route_map_fn=openapi_route_map,
        mcp_component_fn=annotate_openapi_component,
        strict_input_validation=True,
        version=__version__,
    )
    mcp.mount(open_api_mcp)

    return mcp


def main():
    """
    Entry point for the Cortex MCP Server application.

    This function serves as the main entry point that wraps the async main function
    in an asyncio.run() call. It handles top-level exception catching and ensures
    proper cleanup and logging during shutdown.

    Returns:
        None

    Side Effects:
        - Starts the asyncio event loop
        - Logs server startup and shutdown events
        - Handles exceptions and ensures graceful shutdown
    """
    try:
        asyncio.run(async_main(get_config().mcp_transport))
    except Exception as e:
        logger.exception(f"Main loop stopped: {e}")
    finally:
        logger.info("Cortex MCP Server has shut down.")


if __name__ == "__main__":
    main()
