"""Handwritten tools for Cortex exports that return zip or gzip bodies.

The generated catalog reads every response as UTF-8 JSON. Playbook and script
exports are zip archives, and the incident performance export is gzip. Those
operation ids are removed from the catalog and served here.
"""

import base64
import gzip
import io
import json
import logging
import zipfile
import zlib
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field, ValidationError

from entities.exceptions import (
    PAPIAuthenticationError,
    PAPIClientError,
    PAPIClientRequestError,
    PAPIConnectionError,
    PAPIResponseError,
    PAPIServerError,
)
from pkg.client import ResponseBytes, binary_response_error, response_media_type
from pkg.util import create_response
from usecase.base_module import BaseModule
from usecase.fetcher import get_fetcher

logger = logging.getLogger(__name__)

BINARY_EXPORT_OPERATION_IDS = frozenset(
    {
        "post_playbooks_get",
        "post_scripts_get",
        "get_performance_incident_export_by_incident_id",
    }
)

_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"
_ARCHIVE_MEDIA_TYPES = {"application/zip", "application/octet-stream"}
# Keep a zip out of the model context when the archive is larger than this.
_MAX_EXPORT_BYTES = 8 * 1024 * 1024
_MAX_LISTED_FILES = 200
_EXPORT_TIMEOUT_SECONDS = 300

_PAPI_ERRORS = (
    PAPIConnectionError,
    PAPIAuthenticationError,
    PAPIServerError,
    PAPIClientRequestError,
    PAPIResponseError,
    PAPIClientError,
)


class ExportFilter(BaseModel):
    """Name or id filter. The public API rejects every other field."""

    field: Literal["id", "name"] = Field(
        description="Identifies the field the filter matches. Only name and id are supported."
    )
    value: str = Field(description="Value that this filter must match.")


class ExportRequest(BaseModel):
    """JSON body for Get a playbook and Get a script."""

    filter: ExportFilter = Field(description="Filter fields. Required. Only a name or id filter is supported.")


def _gunzip(content: bytes) -> bytes:
    try:
        return gzip.decompress(content)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError(f"gzip response ({len(content)} bytes) could not be decompressed") from exc


def _json_or_text(content: bytes, media: str) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(binary_response_error(media or "application/octet-stream", len(content))) from None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _archive_mime(content: bytes, media: str) -> str:
    if content[:4] == _ZIP_MAGIC or media == "application/zip":
        return "application/zip"
    return media or "application/octet-stream"


def _zip_members(content: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return archive.namelist()
    except zipfile.BadZipFile:
        return []


def _archive_payload(content: bytes, mime: str) -> dict[str, Any]:
    if len(content) > _MAX_EXPORT_BYTES:
        raise ValueError(f"binary response ({mime}, {len(content)} bytes) exceeds the {_MAX_EXPORT_BYTES} byte cap")
    payload: dict[str, Any] = {
        "mimeType": mime,
        "blob": base64.b64encode(content).decode("ascii"),
        "size": len(content),
    }
    if mime == "application/zip":
        names = _zip_members(content)
        payload["files"] = names[:_MAX_LISTED_FILES]
        if len(names) > _MAX_LISTED_FILES:
            payload["files_truncated"] = True
    return payload


def interpret_export_body(
    content: bytes,
    *,
    content_type: str = "",
    content_encoding: str = "",
) -> dict[str, Any]:
    """Turn an export body into JSON or a base64 archive summary.

    Gzip (magic ``1f 8b``, including a body whose ``Content-Encoding`` is gzip
    but was not decoded) is decompressed, then parsed as JSON, then as text.
    A zip archive, or a body whose media type is ``application/zip`` or
    ``application/octet-stream``, is returned as base64 with ``mimeType`` and
    ``size``. httpx already decodes ``Content-Encoding: gzip`` when the header
    is present, so a gzip header without the magic bytes is treated as the
    decoded payload.
    """
    media = response_media_type(content_type)
    if content[:2] == _GZIP_MAGIC:
        return _json_or_text(_gunzip(content), media)
    # Content-Encoding: gzip without the magic means the transport already
    # decoded the body. Parse that payload instead of treating it as an archive.
    if "gzip" in content_encoding.lower():
        return _json_or_text(content, media)
    if content[:4] == _ZIP_MAGIC or media in _ARCHIVE_MEDIA_TYPES:
        return _archive_payload(content, _archive_mime(content, media))
    return _json_or_text(content, media)


def _body_headers(payload: bytes | bytearray) -> tuple[str, str]:
    if isinstance(payload, ResponseBytes):
        return payload.content_type, payload.content_encoding
    return "", ""


def _export_request(value: ExportRequest | dict[str, Any]) -> ExportRequest:
    if isinstance(value, ExportRequest):
        return value
    try:
        return ExportRequest.model_validate(value)
    except ValidationError as exc:
        raise ValueError("request_data.filter is required. The filter accepts only field id or name.") from exc


def _segment_error(value: str, label: str) -> str | None:
    if not value or not value.strip():
        return f"{label} is empty."
    if value != value.strip() or any(char in value for char in "/\\?#%"):
        return f"{label} cannot contain path characters."
    return None


async def _read_export(
    ctx: Context,
    method: str,
    path: str,
    data: dict[str, Any] | None,
    label: str,
) -> str:
    try:
        fetcher = await get_fetcher(ctx)
        payload = await fetcher.send_request(
            path,
            method=method,
            data=data,
            omit_papi_prefix=True,
            raw=True,
            timeout=_EXPORT_TIMEOUT_SECONDS,
        )
        if not isinstance(payload, (bytes, bytearray)):
            return create_response(data={"error": f"{label} response was not a byte payload."}, is_error=True)
        content_type, content_encoding = _body_headers(payload)
        interpreted = interpret_export_body(
            bytes(payload),
            content_type=content_type,
            content_encoding=content_encoding,
        )
        return create_response(data=dict(interpreted))
    except _PAPI_ERRORS as exc:
        logger.exception(f"PAPI error during {label}: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)
    except ValueError as exc:
        logger.warning("%s: %s", label, exc)
        return create_response(data={"error": str(exc)}, is_error=True)
    except Exception as exc:
        logger.exception(f"Unexpected error during {label}: {exc}")
        return create_response(data={"error": str(exc)}, is_error=True)


async def post_playbooks_get(
    ctx: Context,
    request_data: Annotated[
        ExportRequest,
        Field(description="Playbook lookup. request_data.filter is required and accepts only field name or id."),
    ],
) -> str:
    """
    Side effects: none. This is a read-only Cortex API call (POST /public_api/v1/playbooks/get).
    Get a playbook. The public API returns a zip archive, which this tool returns as base64.
    """
    try:
        request = _export_request(request_data)
    except ValueError as exc:
        return create_response(data={"error": str(exc)}, is_error=True)
    body = {"request_data": {"filter": {"field": request.filter.field, "value": request.filter.value}}}
    return await _read_export(ctx, "POST", "/public_api/v1/playbooks/get", body, "playbook export")


async def post_scripts_get(
    ctx: Context,
    request_data: Annotated[
        ExportRequest,
        Field(description="Script lookup. request_data.filter is required and accepts only field name or id."),
    ],
) -> str:
    """
    Side effects: none. This is a read-only Cortex API call (POST /public_api/v1/scripts/get).
    Get a script. The public API returns a zip archive, which this tool returns as base64.
    """
    try:
        request = _export_request(request_data)
    except ValueError as exc:
        return create_response(data={"error": str(exc)}, is_error=True)
    body = {"request_data": {"filter": {"field": request.filter.field, "value": request.filter.value}}}
    return await _read_export(ctx, "POST", "/public_api/v1/scripts/get", body, "script export")


async def get_performance_incident_export_by_incident_id(
    ctx: Context,
    incident_id: Annotated[str, Field(description="Incident id whose history and workplan are exported.")],
) -> str:
    """
    Side effects: none. This is a read-only Cortex API call
    (GET /xsoar/public/v1/performance/incident/export/{incident_id}).
    Export an incident's history and workplan. A gzip body is decompressed and parsed as JSON.
    """
    if error := _segment_error(incident_id, "Incident id"):
        return create_response(data={"error": error}, is_error=True)
    path = f"/xsoar/public/v1/performance/incident/export/{incident_id}"
    return await _read_export(ctx, "GET", path, None, "incident performance export")


class ContentExportsModule(BaseModule):
    """Zip and gzip Cortex exports that the OpenAPI catalog cannot decode."""

    def register_tools(self):
        self._add_tool(post_playbooks_get)
        self._add_tool(post_scripts_get)
        self._add_tool(get_performance_incident_export_by_incident_id)

    def register_resources(self):
        pass

    def __init__(self, mcp: FastMCP):
        super().__init__(mcp)
