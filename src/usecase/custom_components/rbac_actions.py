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
async def set_user_role(
    ctx: Context,
    user_emails: Annotated[list[str], Field(description="List of user email addresses to assign the role to.")],
    role_name: Annotated[
        str, Field(description="Name of the role to assign. Use get_rbac_roles to look up valid role names.")
    ],
) -> str:
    """
    Assign a role to one or more users on the Cortex platform.
    This is a permission-changing action — it directly affects what the target users can
    access on the platform. Consider using get_rbac_roles first to confirm the exact role
    name, and get_rbac_user_group / get_rbac_users to confirm the target users.

    Args:
        ctx: The FastMCP context.
        user_emails: Email addresses of the users to assign the role to.
        role_name: Name of the role to assign.

    Returns:
        JSON response indicating success or failure of the role assignment.
    """
    payload = {
        "request_data": {
            "user_emails": user_emails,
            "role_name": role_name,
        }
    }

    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request("rbac/set_user_role", data=payload)
        return create_response(data=response_data)
    except _PAPI_ERRORS as e:
        logger.exception(f"PAPI error while setting role '{role_name}' for users {user_emails}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)
    except Exception as e:
        logger.exception(f"Unexpected error while setting role '{role_name}' for users {user_emails}: {e}")
        return create_response(data={"error": str(e)}, is_error=True)


class RbacActionsModule(BaseModule):
    """
    Module providing write actions for RBAC (role-based access control) management.

    Tools provided:
        - set_user_role: Assign a role to one or more users. Permission-changing.
    """

    def register_tools(self):
        self._add_tool(set_user_role)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
