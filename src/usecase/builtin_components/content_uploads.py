import io
import logging
import zipfile
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

logger = logging.getLogger(__name__)

_PAPI_ERRORS = (
    PAPIConnectionError,
    PAPIAuthenticationError,
    PAPIServerError,
    PAPIClientRequestError,
    PAPIResponseError,
    PAPIClientError,
)

_MAX_CONTENT_CHARS = 1_000_000


def zip_named_text(member_name: str, content: str) -> bytes:
    """Pack text into a zip archive, which is the body the public API expects."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


def _validate_upload_text(content: str, label: str) -> str | None:
    if not content or not content.strip():
        return f"{label} content is empty."
    if "\x00" in content:
        return f"{label} content must be text, not a binary payload."
    if len(content) > _MAX_CONTENT_CHARS:
        return f"{label} content exceeds {_MAX_CONTENT_CHARS} characters."
    return None


async def _upload_zip(ctx: Context, path: str, member_name: str, content: str, label: str) -> str:
    error = _validate_upload_text(content, label)
    if error:
        return create_response(data={"error": error}, is_error=True)

    payload = zip_named_text(member_name, content)
    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request(
            path,
            method="POST",
            files={"file": (f"{member_name}.zip", payload, "application/zip")},
            omit_papi_prefix=True,
        )
        return create_response(data=response_data)
    except _PAPI_ERRORS as exc:
        logger.exception(f"PAPI error while uploading {label}: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)
    except Exception as exc:
        logger.exception(f"Unexpected error while uploading {label}: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)


async def insert_script(
    ctx: Context,
    script_yaml: Annotated[
        str, Field(description="Script definition in YAML. The server zips this text and uploads it.")
    ],
) -> str:
    """
    Upload a script definition to the Cortex script library.

    Side effects: this operation creates or replaces a script in the tenant
    (POST /public_api/v1/scripts/insert). Confirm the script content before calling.
    The public API expects the YAML inside a zip file; this tool builds that archive.
    """
    return await _upload_zip(ctx, "/public_api/v1/scripts/insert", "script.yml", script_yaml, "Script")


async def insert_playbook(
    ctx: Context,
    playbook_yaml: Annotated[
        str, Field(description="Playbook definition in YAML. The server zips this text and uploads it.")
    ],
) -> str:
    """
    Upload a playbook definition to the Cortex playbook library.

    Side effects: this operation creates or replaces a playbook in the tenant
    (POST /public_api/v1/playbooks/insert). Confirm the playbook content before calling.
    The public API expects the YAML inside a zip file; this tool builds that archive.
    """
    return await _upload_zip(ctx, "/public_api/v1/playbooks/insert", "playbook.yml", playbook_yaml, "Playbook")


class ContentUploadsModule(BaseModule):
    """Multipart script and playbook uploads documented by the Cortex public API."""

    def register_tools(self):
        self._add_tool(insert_script)
        self._add_tool(insert_playbook)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
