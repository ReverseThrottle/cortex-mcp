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

_VALID_STATUSES = {"new", "under_investigation", "resolved", "closed"}
_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


@require_flag("write_tools_enabled")
async def update_case(
    ctx: Context,
    case_ids: Annotated[list[int], Field(description="List of case IDs to update. All supplied cases receive the same update.")],
    comment: Annotated[Optional[str], Field(description="Comment or note to add to the case, e.g. 'Escalated to Tier 2 for further investigation'", default=None)] = None,
    status: Annotated[Optional[str], Field(description="New case status. Allowed values: new, under_investigation, resolved, closed", default=None)] = None,
    assigned_user_mail: Annotated[Optional[str], Field(description="Email address of the analyst to assign the case to", default=None)] = None,
    severity: Annotated[Optional[str], Field(description="New severity level. Allowed values: low, medium, high, critical", default=None)] = None,
) -> str:
    """
    Update one or more cases on the Cortex platform.
    Supports adding comments/notes, changing status, reassigning to an analyst, and changing severity.
    At least one of comment, status, assigned_user_mail, or severity must be provided.

    Args:
        ctx: The FastMCP context.
        case_ids: List of case IDs to update (integers).
        comment: Text of the comment or note to add.
        status: New status — one of: new, under_investigation, resolved, closed.
        assigned_user_mail: Email of the analyst to assign to.
        severity: New severity — one of: low, medium, high, critical.

    Returns:
        JSON response indicating success or failure of the update.
    """
    update_data: dict = {}

    if comment:
        update_data["comment"] = comment
    if status:
        if status not in _VALID_STATUSES:
            return create_response(
                data={"error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"},
                is_error=True,
            )
        update_data["status"] = status
    if assigned_user_mail:
        update_data["assigned_user_mail"] = assigned_user_mail
    if severity:
        if severity not in _VALID_SEVERITIES:
            return create_response(
                data={"error": f"Invalid severity '{severity}'. Must be one of: {', '.join(sorted(_VALID_SEVERITIES))}"},
                is_error=True,
            )
        update_data["severity"] = severity

    if not update_data:
        return create_response(
            data={"error": "At least one of comment, status, assigned_user_mail, or severity must be provided."},
            is_error=True,
        )

    payload = {
        "request_data": {
            "case_id_list": case_ids,
            "update_data": update_data,
        }
    }

    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request("case/update/", data=payload)
        return create_response(data=response_data)
    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while updating cases {case_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while updating cases {case_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)


class CaseActionsModule(BaseModule):
    """
    Module providing write actions for case management.

    Tools provided:
        - update_case: Add notes, change status, reassign, or update severity on one or more cases.
    """

    def register_tools(self):
        self._add_tool(update_case)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
