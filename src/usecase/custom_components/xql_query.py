import asyncio
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

_POLL_INTERVAL_SECONDS = 2
_MAX_POLL_ATTEMPTS = 15  # 30 second max wait


async def run_xql_query(
    ctx: Context,
    query: Annotated[str, Field(description=(
        "XQL query to execute. Examples: "
        "'dataset=xdr_data | filter event_type=ENUM.PROCESS | fields actor_process_image_name, agent_hostname | limit 20' "
        "or 'dataset=xdr_data | filter agent_hostname=\"WIN-123\" | limit 50'"
    ))],
    timeframe: Annotated[Optional[dict], Field(description=(
        "Optional timeframe for the query. "
        "Relative: {'relativeTime': 'last_1_hour'} — valid values: last_15_minutes, last_30_minutes, last_1_hour, last_3_hours, last_6_hours, last_12_hours, last_24_hours, last_2_days, last_7_days. "
        "Absolute: {'from': <epoch_ms>, 'to': <epoch_ms>}. "
        "Defaults to last 24 hours if omitted."
    ), default=None)] = None,
    limit: Annotated[int, Field(description="Maximum number of result rows to return. Max 1000.", default=100, ge=1, le=1000)] = 100,
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (POST /public_api/v1/xql/start_xql_query).
    It starts a query and consumes XQL quota. Confirm the query before calling.
    Execute an XQL query against the Cortex platform and return the results.
    XQL (Extended Query Language) enables powerful threat hunting and investigation across
    all Cortex data sources. The query runs asynchronously — this tool starts the query,
    polls until complete, and returns the full result set.

    Args:
        ctx: The FastMCP context.
        query: XQL query string to execute.
        timeframe: Optional time range for the query. Defaults to last 24 hours.
        limit: Maximum rows to return (1–1000, default 100).

    Returns:
        JSON response containing query results or an error message.
    """
    start_payload: dict = {
        "request_data": {
            "query": query,
        }
    }
    if timeframe:
        start_payload["request_data"]["timeframe"] = timeframe

    try:
        fetcher = await get_fetcher(ctx)

        # Step 1: Start the query
        start_response = await fetcher.send_request("xql/start_xql_query/", data=start_payload)
        # The API returns {"reply": "<execution_id>"} — reply is the ID string directly
        reply = start_response.get("reply")
        if isinstance(reply, str):
            execution_id = reply
        elif isinstance(reply, dict):
            execution_id = reply.get("execution_id")
        else:
            execution_id = None
        if not execution_id:
            logger.error(f"No execution_id in XQL start response: {start_response}")
            return create_response(
                data={"error": "Failed to start XQL query: no execution_id returned", "response": start_response},
                is_error=True,
            )

        logger.info(f"XQL query started with execution_id={execution_id}")

        # Step 2: Poll for results
        results_payload = {
            "request_data": {
                "query_id": execution_id,
                "pending_duration": _POLL_INTERVAL_SECONDS,
                "limit": limit,
                "format": "json",
            }
        }

        for attempt in range(1, _MAX_POLL_ATTEMPTS + 1):
            results_response = await fetcher.send_request("xql/get_xql_query_results/", data=results_payload)
            reply = results_response.get("reply", {})
            status = reply.get("status")

            if status == "SUCCESS":
                logger.info(f"XQL query {execution_id} completed on poll attempt {attempt}")
                return create_response(data=results_response)

            if status == "FAILED":
                error_msg = reply.get("error", "Unknown error")
                logger.error(f"XQL query {execution_id} failed: {error_msg}")
                return create_response(
                    data={"error": f"XQL query failed: {error_msg}", "execution_id": execution_id},
                    is_error=True,
                )

            if status == "PENDING":
                logger.info(f"XQL query {execution_id} still pending (attempt {attempt}/{_MAX_POLL_ATTEMPTS})")
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue

            # Unexpected status
            logger.error(f"XQL query {execution_id} returned unexpected status: {status}")
            return create_response(
                data={"error": f"Unexpected query status: {status}", "response": results_response},
                is_error=True,
            )

        return create_response(
            data={"error": f"XQL query {execution_id} timed out after {_MAX_POLL_ATTEMPTS * _POLL_INTERVAL_SECONDS} seconds"},
            is_error=True,
        )

    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while running XQL query: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while running XQL query: {e}")
        return create_response(data={"error": str(e)}, is_error=True)


class XQLQueryModule(BaseModule):
    """
    Module for executing XQL queries against the Cortex platform.

    Tools provided:
        - run_xql_query: Execute an XQL query and return results, handling async polling internally.
    """

    def register_tools(self):
        self._add_tool(run_xql_query)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
