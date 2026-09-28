import base64
import binascii
import logging
from typing import Annotated, Literal, Optional

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

_MAX_CONTENT_CHARS = 1_000_000


def _text_error(content: str, label: str) -> str | None:
    if not content or not content.strip():
        return f"{label} is empty."
    if "\x00" in content:
        return f"{label} must be text, not a binary payload."
    if len(content) > _MAX_CONTENT_CHARS:
        return f"{label} exceeds {_MAX_CONTENT_CHARS} characters."
    return None


def _file_bytes(content: str, encoding: str, label: str) -> tuple[bytes | None, str | None]:
    """Decode an upload. utf-8 rejects NUL. base64 accepts binary files such as a zip or screenshot."""
    if encoding == "utf-8":
        error = _text_error(content, label)
        if error:
            return None, error
        return content.encode("utf-8"), None
    if encoding != "base64":
        return None, f"{label} encoding must be utf-8 or base64."
    compact = "".join(content.split())
    if not compact:
        return None, f"{label} is empty."
    padded = compact + ("=" * ((-len(compact)) % 4))
    try:
        payload = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None, f"{label} is not valid base64."
    if not payload:
        return None, f"{label} is empty."
    if len(payload) > _MAX_CONTENT_CHARS:
        return None, f"{label} exceeds {_MAX_CONTENT_CHARS} bytes."
    return payload, None


def _form_file(value: str | None) -> tuple[None, str] | None:
    if value is None:
        return None
    return (None, value)


async def _post_multipart(ctx: Context, path: str, files: dict, label: str) -> str:
    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request(path, method="POST", files=files, omit_papi_prefix=True)
        return create_response(data=response_data)
    except _PAPI_ERRORS as exc:
        logger.exception(f"PAPI error during {label}: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)
    except Exception as exc:
        logger.exception(f"Unexpected error during {label}: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)


async def post_playbook_save_yaml(
    ctx: Context,
    playbook_yaml: Annotated[
        str, Field(description="Playbook YAML. The server uploads it as the multipart file field.")
    ],
    file_name: Annotated[
        str, Field(description="File name sent with the upload.", default="playbook.yml")
    ] = "playbook.yml",
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (POST /xsoar/public/v1/playbook/save/yaml).
    It uploads a playbook from a YAML file. Confirm the content before calling.
    """
    error = _text_error(playbook_yaml, "Playbook YAML")
    if error:
        return create_response(data={"error": error}, is_error=True)
    files = {"file": (file_name, playbook_yaml.encode("utf-8"), "application/yaml")}
    return await _post_multipart(ctx, "/xsoar/public/v1/playbook/save/yaml", files, "playbook YAML upload")


async def post_entry_upload_by_incident_id(
    ctx: Context,
    incident_id: Annotated[str, Field(description="Incident id that receives the War Room entry.")],
    file_content: Annotated[
        str,
        Field(
            description="File contents. Plain text when content_encoding is utf-8. Standard base64 when it is base64."
        ),
    ],
    content_encoding: Annotated[
        Literal["utf-8", "base64"],
        Field(
            description="utf-8 for text. base64 for a binary file such as a screenshot or zip.",
            default="utf-8",
        ),
    ] = "utf-8",
    file_name: Annotated[str, Field(description="File name.", default="upload.txt")] = "upload.txt",
    file_comment: Annotated[Optional[str], Field(description="Comment stored with the entry.", default=None)] = None,
    is_note_entry: Annotated[Optional[str], Field(description="Whether the entry is a note.", default=None)] = None,
    show_media_files: Annotated[Optional[str], Field(description="Whether to show media files.", default=None)] = None,
    tags: Annotated[Optional[str], Field(description="Tags for the entry.", default=None)] = None,
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (POST /xsoar/public/v1/entry/upload/{incident_id}).
    It uploads content to an incident War Room entry. Text uses utf-8. A screenshot or zip uses base64.
    Confirm the incident and content before calling.
    """
    payload, error = _file_bytes(file_content, content_encoding, "File content")
    incident_error = _text_error(incident_id, "Incident id")
    if error or incident_error or payload is None:
        return create_response(data={"error": error or incident_error}, is_error=True)
    files = {
        "file": (file_name, payload, "application/octet-stream"),
    }
    for field_name, value in (
        ("fileComment", file_comment),
        ("isNoteEntry", is_note_entry),
        ("showMediaFiles", show_media_files),
        ("tags", tags),
    ):
        part = _form_file(value)
        if part is not None:
            files[field_name] = part
    path = f"/xsoar/public/v1/entry/upload/{incident_id}"
    return await _post_multipart(ctx, path, files, "War Room upload")


async def post_incident_upload_by_incident_id(
    ctx: Context,
    incident_id: Annotated[str, Field(description="Incident id that receives the file.")],
    file_content: Annotated[
        str,
        Field(
            description="File contents. Plain text when content_encoding is utf-8. Standard base64 when it is base64."
        ),
    ],
    content_encoding: Annotated[
        Literal["utf-8", "base64"],
        Field(
            description="utf-8 for text. base64 for a binary file such as a screenshot or zip.",
            default="utf-8",
        ),
    ] = "utf-8",
    file_name: Annotated[Optional[str], Field(description="File name.", default=None)] = None,
    file_comment: Annotated[Optional[str], Field(description="Comment to add to the file.", default=None)] = None,
    field: Annotated[
        Optional[str],
        Field(
            description="Incident field that holds the attachment. Defaults to attachment when omitted.", default=None
        ),
    ] = None,
    show_media_file: Annotated[Optional[bool], Field(description="Whether to show media files.", default=None)] = None,
    last: Annotated[
        Optional[bool],
        Field(
            description="When true, creates an investigation. Used when uploading after creating an incident.",
            default=None,
        ),
    ] = None,
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (POST /xsoar/public/v1/incident/upload/{incident_id}).
    It uploads a file to an incident. Text uses utf-8. A screenshot or zip uses base64.
    Confirm the incident and file before calling.
    """
    payload, error = _file_bytes(file_content, content_encoding, "File content")
    incident_error = _text_error(incident_id, "Incident id")
    if error or incident_error or payload is None:
        return create_response(data={"error": error or incident_error}, is_error=True)
    files = {"file": (file_name or "upload.bin", payload, "application/octet-stream")}
    for field_name, value in (("fileName", file_name), ("fileComment", file_comment), ("field", field)):
        part = _form_file(value)
        if part is not None:
            files[field_name] = part
    if show_media_file is not None:
        files["showMediaFile"] = (None, "true" if show_media_file else "false")
    if last is not None:
        files["last"] = (None, "true" if last else "false")
    path = f"/xsoar/public/v1/incident/upload/{incident_id}"
    return await _post_multipart(ctx, path, files, "incident file upload")


async def post_inv_playbook_task_complete(
    ctx: Context,
    investigation_id: Annotated[str, Field(description="Investigation ID.")],
    file_comment: Annotated[str, Field(description="Comment about the file.")],
    task_id: Annotated[str, Field(description="Task ID.")],
    task_input: Annotated[str, Field(description="Task input.")],
    file_name: Annotated[Optional[str], Field(description="File name.", default=None)] = None,
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (POST /xsoar/public/v1/inv-playbook/task/complete).
    It completes a playbook task. Confirm the investigation and task before calling.
    """
    for label, value in (
        ("Investigation ID", investigation_id),
        ("File comment", file_comment),
        ("Task ID", task_id),
        ("Task input", task_input),
    ):
        error = _text_error(value, label)
        if error:
            return create_response(data={"error": error}, is_error=True)
    files = {
        "investigationId": (None, investigation_id),
        "fileComment": (None, file_comment),
        "taskId": (None, task_id),
        "taskInput": (None, task_input),
    }
    if file_name:
        files["fileName"] = (None, file_name)
    return await _post_multipart(ctx, "/xsoar/public/v1/inv-playbook/task/complete", files, "playbook task complete")


async def put_settings_credentials(
    ctx: Context,
    credential_fields: Annotated[
        dict,
        Field(
            description=(
                "Credential object to create or update. The public reference describes the body only as "
                "credential fields and does not list properties. Include the fields the tenant expects, "
                "such as name and user."
            )
        ),
    ],
) -> str:
    """
    Side effects: this operation changes Cortex tenant state (PUT /xsoar/public/v1/settings/credentials).
    It creates or updates integration credentials. Confirm the target before calling.
    """
    if not isinstance(credential_fields, dict) or not credential_fields:
        return create_response(data={"error": "credential_fields must be a non-empty object."}, is_error=True)
    try:
        fetcher = await get_fetcher(ctx)
        response_data = await fetcher.send_request(
            "/xsoar/public/v1/settings/credentials",
            method="PUT",
            data=credential_fields,
            omit_papi_prefix=True,
        )
        return create_response(data=response_data)
    except _PAPI_ERRORS as exc:
        logger.exception(f"PAPI error while saving credentials: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)
    except Exception as exc:
        logger.exception(f"Unexpected error while saving credentials: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)


class XsoarActionsModule(BaseModule):
    """XSOAR 8 uploads and credential writes whose public bodies are not JSON."""

    def register_tools(self):
        if not get_config().write_tools_enabled:
            return
        self._add_tool(post_playbook_save_yaml)
        self._add_tool(post_entry_upload_by_incident_id)
        self._add_tool(post_incident_upload_by_incident_id)
        self._add_tool(post_inv_playbook_task_complete)
        self._add_tool(put_settings_credentials)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
