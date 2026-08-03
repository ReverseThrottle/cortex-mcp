import logging
from typing import Annotated

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
from usecase.write_gate import require_flag

logger = logging.getLogger(__name__)

_PAPI_ERRORS = (
    PAPIConnectionError,
    PAPIAuthenticationError,
    PAPIServerError,
    PAPIClientRequestError,
    PAPIResponseError,
    PAPIClientError,
)


@require_flag("write_tools_enabled")
async def update_issue(
    ctx: Context,
    issue_ids: Annotated[
        list[str],
        Field(
            description="List of issue/alert IDs to update. All supplied issues receive the same update. Use get_issues to look up IDs."
        ),
    ],
    comment: Annotated[
        str | None,
        Field(description="Comment or note to add to the issue, e.g. 'Confirmed false positive'", default=None),
    ] = None,
    status: Annotated[
        str | None,
        Field(
            description="New issue status. Example value seen in Cortex docs: 'resolved_other'. Not validated client-side — the platform will reject unsupported values.",
            default=None,
        ),
    ] = None,
    severity: Annotated[
        str | None,
        Field(
            description="New severity level. Example values: low, medium, high, critical. Not validated client-side.",
            default=None,
        ),
    ] = None,
) -> str:
    """
    Update one or more issues/alerts on the Cortex platform (legacy "alerts" API — issues
    and alerts are the same underlying resource under two naming generations).
    Supports adding comments/notes, changing status, and changing severity.
    At least one of comment, status, or severity must be provided.

    Args:
        ctx: The FastMCP context.
        issue_ids: List of issue/alert IDs to update.
        comment: Text of the comment or note to add.
        severity: New severity level.
        status: New status.

    Returns:
        JSON response indicating success or failure of the update.
    """
    update_data: dict = {}

    if comment:
        update_data["comment"] = comment
    if status:
        update_data["status"] = status
    if severity:
        update_data["severity"] = severity

    if not update_data:
        return create_response(
            data={"error": "At least one of comment, status, or severity must be provided."},
            is_error=True,
        )

    payload = {
        "request_data": {
            "alert_id_list": issue_ids,
            "update_data": update_data,
        }
    }

    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request("alerts/update_alerts", data=payload)
        return create_response(data=response_data)
    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while updating issues {issue_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while updating issues {issue_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)


class IssueActionsModule(BaseModule):
    """
    Module providing write actions for issue/alert management.

    Tools provided:
        - update_issue: Add notes, change status, or update severity on one or more issues.
    """

    def register_tools(self):
        self._add_tool(update_issue)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
