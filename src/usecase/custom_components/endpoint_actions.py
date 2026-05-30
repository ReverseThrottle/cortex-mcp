import logging
from typing import Annotated, Optional

from fastmcp import Context, FastMCP
from pydantic import Field

from entities.exceptions import (
    PAPIAuthenticationError,
    PAPIClientError,
    PAPIClientRequestError,
    PAPIConnectionError,
    PAPIResponseError,
    PAPIServerError,
)
from pkg.util import create_response
from usecase.base_module import BaseModule
from usecase.fetcher import get_fetcher

logger = logging.getLogger(__name__)

_PAPI_ERRORS = (
    PAPIConnectionError,
    PAPIAuthenticationError,
    PAPIServerError,
    PAPIClientRequestError,
    PAPIResponseError,
    PAPIClientError,
)


async def isolate_endpoint(
    ctx: Context,
    endpoint_ids: Annotated[list[str], Field(description="List of endpoint IDs to isolate. Use get_filtered_endpoints to look up IDs by hostname first if needed.")],
    comment: Annotated[Optional[str], Field(description="Reason for isolating the endpoint, e.g. 'Suspected compromise - isolating for investigation'", default=None)] = None,
) -> str:
    """
    Isolate one or more endpoints from the network.
    Blocks all network traffic on the endpoint except communication to the Cortex XDR agent.
    Use this when an endpoint may be compromised to prevent lateral movement or data exfiltration.
    To find endpoint IDs by hostname, use get_filtered_endpoints first.

    Args:
        ctx: The FastMCP context.
        endpoint_ids: List of endpoint IDs to isolate.
        comment: Optional reason or justification for the isolation action.

    Returns:
        JSON response indicating success or failure of the isolation request.
    """
    payload: dict = {
        "request_data": {
            "filters": [
                {
                    "field": "endpoint_id_list",
                    "operator": "in",
                    "value": endpoint_ids,
                }
            ],
        }
    }
    if comment:
        payload["request_data"]["comment"] = comment

    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request("endpoints/isolate/", data=payload)
        return create_response(data=response_data)
    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while isolating endpoints {endpoint_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while isolating endpoints {endpoint_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)


async def unisolate_endpoint(
    ctx: Context,
    endpoint_ids: Annotated[list[str], Field(description="List of endpoint IDs to unisolate (restore network access). Use get_filtered_endpoints to look up IDs by hostname first if needed.")],
    comment: Annotated[Optional[str], Field(description="Reason for unisolating the endpoint, e.g. 'Investigation complete - endpoint cleared'", default=None)] = None,
) -> str:
    """
    Unisolate one or more endpoints, restoring their network connectivity.
    Use this after an endpoint has been investigated and confirmed safe, or when isolation
    was applied in error.
    To find endpoint IDs by hostname, use get_filtered_endpoints first.

    Args:
        ctx: The FastMCP context.
        endpoint_ids: List of endpoint IDs to unisolate.
        comment: Optional reason or justification for restoring network access.

    Returns:
        JSON response indicating success or failure of the unisolation request.
    """
    payload: dict = {
        "request_data": {
            "filters": [
                {
                    "field": "endpoint_id_list",
                    "operator": "in",
                    "value": endpoint_ids,
                }
            ],
        }
    }
    if comment:
        payload["request_data"]["comment"] = comment

    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request("endpoints/unisolate/", data=payload)
        return create_response(data=response_data)
    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while unisolating endpoints {endpoint_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while unisolating endpoints {endpoint_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)


class EndpointActionsModule(BaseModule):
    """
    Module providing write actions for endpoint management.

    Tools provided:
        - isolate_endpoint: Cut an endpoint off from the network to contain a threat.
        - unisolate_endpoint: Restore network access to a previously isolated endpoint.
    """

    def register_tools(self):
        self._add_tool(isolate_endpoint)
        self._add_tool(unisolate_endpoint)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
