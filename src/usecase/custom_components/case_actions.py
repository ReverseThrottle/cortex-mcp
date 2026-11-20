import logging
from typing import Annotated, Optional

from fastmcp import Context, FastMCP
from pydantic import Field

from config.config import get_config
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

# Spec (post_case_update_by_case_id): user_severity is a lowercase enum; an
# empty string clears the override. status_progress is deliberately NOT
# whitelisted here: it is case-insensitive and tenants can configure custom
# statuses, so only the API knows the full valid set (it lists the valid
# options in its error response when a value is invalid).
_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


async def update_case(
    ctx: Context,
    case_ids: Annotated[
        list[int],
        Field(
            description=(
                "List of case IDs to update. Each case gets its own request to the per-case endpoint "
                "(POST /public_api/v1/case/update/{case_id}); all supplied cases receive the same update."
            )
        ),
    ],
    notes: Annotated[
        Optional[str],
        Field(
            description="Free-text note to add to the case, e.g. 'Escalated to Tier 2 for further investigation'",
            default=None,
        ),
    ] = None,
    status: Annotated[
        Optional[str],
        Field(
            description=(
                "New case status. Built-in values: 'New', 'In Progress', 'Resolved' (case-insensitive). "
                "Tenant-custom statuses are also accepted; the API returns the valid options if the value is invalid."
            ),
            default=None,
        ),
    ] = None,
    assigned_user: Annotated[
        Optional[str], Field(description="Email address of the Cortex user to assign the case to", default=None)
    ] = None,
    severity: Annotated[
        Optional[str],
        Field(
            description="New severity level: low, medium, high, critical. Pass an empty string to clear the severity.",
            default=None,
        ),
    ] = None,
    resolve_reason: Annotated[
        Optional[str],
        Field(
            description=(
                "Resolution reason; required when status is 'Resolved'. Built-in values include 'Resolved - Known Issue', "
                "'Resolved - Duplicate Case', 'Resolved - False Positive', 'Resolved - Other', 'Resolved - True Positive', "
                "'Resolved - Security Testing'; tenant-custom reasons are also accepted."
            ),
            default=None,
        ),
    ] = None,
    resolve_comment: Annotated[
        Optional[str],
        Field(
            description="Free-text comment added when resolving the case. Only applies when status is 'Resolved'.",
            default=None,
        ),
    ] = None,
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (POST /public_api/v1/case/update/{case_id}).
    It can update case status, assignment, severity, or notes. Confirm the target before calling.
    Update one or more cases on the Cortex platform.
    The vendor API only defines a per-case update endpoint, so each case ID is sent as its own
    request (no bulk case_id_list body field). Cases are updated sequentially; a failure on one
    case does not stop the others, and the response reports each case's outcome individually.
    Supports adding notes, changing status, reassigning to an analyst, changing severity, and
    resolving with a reason and comment.
    At least one of notes, status, assigned_user, severity, resolve_reason, or resolve_comment
    must be provided.

    Args:
        ctx: The FastMCP context.
        case_ids: List of case IDs to update (integers).
        notes: Free-text note to add to the case (built-in 'notes' field).
        status: New status — built-in 'New', 'In Progress', 'Resolved' (case-insensitive), or a
            tenant-custom status. Sent as 'status_progress'.
        assigned_user: Email of the Cortex user to assign to (built-in 'assigned_user' field).
        severity: New severity — low, medium, high, critical; empty string clears it.
            Sent as 'user_severity'.
        resolve_reason: Resolution reason; required when status is 'Resolved'.
        resolve_comment: Free-text resolution comment; only applies when status is 'Resolved'.

    Returns:
        JSON response with a per-case 'results' map (case ID -> API reply or error) and an
        overall success flag that is false when any case failed.
    """
    if not case_ids:
        return create_response(data={"error": "case_ids must contain at least one case ID."}, is_error=True)

    update_data: dict = {}

    if notes:
        update_data["notes"] = notes
    if status:
        update_data["status_progress"] = status
    if assigned_user:
        update_data["assigned_user"] = assigned_user
    if severity is not None:
        normalized_severity = severity.lower()
        if normalized_severity and normalized_severity not in _VALID_SEVERITIES:
            return create_response(
                data={
                    "error": f"Invalid severity '{severity}'. Must be one of: {', '.join(sorted(_VALID_SEVERITIES))}"
                },
                is_error=True,
            )
        update_data["user_severity"] = normalized_severity

    resolving = (status or "").strip().lower() == "resolved"
    if resolve_reason:
        if not resolving:
            return create_response(
                data={"error": "resolve_reason only applies when status is 'Resolved'."}, is_error=True
            )
        update_data["resolve_reason"] = resolve_reason
    if resolve_comment:
        if not resolving:
            return create_response(
                data={"error": "resolve_comment only applies when status is 'Resolved'."}, is_error=True
            )
        update_data["resolve_comment"] = resolve_comment
    if resolving and not resolve_reason:
        return create_response(data={"error": "resolve_reason is required when status is 'Resolved'."}, is_error=True)

    if not update_data:
        return create_response(
            data={
                "error": (
                    "At least one of notes, status, assigned_user, severity, resolve_reason, "
                    "or resolve_comment must be provided."
                )
            },
            is_error=True,
        )

    payload = {"request_data": {"update_data": update_data}}

    try:
        fetcher = await get_fetcher(ctx)
    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while preparing case updates for {case_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while preparing case updates for {case_ids}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)

    results: dict = {}
    failures = 0
    for case_id in case_ids:
        try:
            results[str(case_id)] = await fetcher.send_request(f"case/update/{case_id}", data=payload)
        except Exception as e:
            logger.exception(f"Error while updating case {case_id}: {e}")
            results[str(case_id)] = {"error": str(e)}
            failures += 1

    return create_response(data={"results": results}, is_error=failures > 0)


class CaseActionsModule(BaseModule):
    """
    Module providing write actions for case management.

    Tools provided:
        - update_case: Add notes, change status, reassign, or update severity on one or more cases.
    """

    def register_tools(self):
        if get_config().write_tools_enabled:
            self._add_tool(update_case)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
