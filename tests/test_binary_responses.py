import base64
import gzip
import io
import json
import zipfile

import httpx2 as httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.providers.openapi.routing import MCPType

from config.config import reload_config
from entities.exceptions import PAPIResponseError
from main import openapi_route_map
from pkg.client import PAPIClient, binary_response_error
from pkg.util import bundle_openapi_from_folders
from usecase.builtin_components.content_exports import (
    _MAX_EXPORT_BYTES,
    BINARY_EXPORT_OPERATION_IDS,
    ContentExportsModule,
    ExportFilter,
    ExportRequest,
    get_performance_incident_export_by_incident_id,
    interpret_export_body,
    post_playbooks_get,
    post_scripts_get,
)
from usecase.module_util import discover_and_register_modules

_CATALOG_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "binary", "version": "1"},
    "paths": {
        "/public_api/v1/demo/get": {
            "post": {
                "operationId": "post_demo_get",
                "description": "Side effects: none. This is a read-only Cortex API call.",
                "responses": {"200": {"description": "Successful response"}},
            }
        }
    },
}


def _zip_bytes(member: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


class _RawFetcher:
    def __init__(self, handler):
        self.client = PAPIClient(
            "https://api.example.invalid",
            "tenant-secret",
            "42",
            transport=httpx.MockTransport(handler),
        )

    async def send_request(
        self,
        path,
        method="POST",
        data=None,
        headers=None,
        omit_papi_prefix=False,
        stream=False,
        files=None,
        raw=False,
        timeout=None,
    ):
        assert raw is True
        return await self.client.request(method, path, json=data if isinstance(data, dict) else None, raw=True)

    async def aclose(self):
        await self.client.aclose()


def _install(monkeypatch, handler) -> _RawFetcher:
    fetcher = _RawFetcher(handler)

    async def fake_get_fetcher(ctx):
        return fetcher

    monkeypatch.setattr("usecase.builtin_components.content_exports.get_fetcher", fake_get_fetcher)
    return fetcher


def _request(field: str, value: str) -> ExportRequest:
    return ExportRequest(filter=ExportFilter(field=field, value=value))


@pytest.mark.asyncio
async def test_playbook_zip_returns_base64_mime_and_size(monkeypatch):
    archive = _zip_bytes("playbook.yml", "name: demo\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/public_api/v1/playbooks/get"
        assert json.loads(request.content) == {"request_data": {"filter": {"field": "id", "value": "pb-1"}}}
        assert request.headers["authorization"] == "tenant-secret"
        return httpx.Response(200, content=archive, headers={"content-type": "application/zip"})

    fetcher = _install(monkeypatch, handler)
    payload = json.loads(await post_playbooks_get(None, _request("id", "pb-1")))
    await fetcher.aclose()

    assert payload["mimeType"] == "application/zip"
    assert base64.b64decode(payload["blob"]) == archive
    assert payload["size"] == len(archive)
    assert payload["files"] == ["playbook.yml"]
    assert payload["success"] == "true"


@pytest.mark.asyncio
async def test_script_zip_round_trips(monkeypatch):
    archive = _zip_bytes("script.yml", "name: lookup\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/public_api/v1/scripts/get"
        assert json.loads(request.content)["request_data"]["filter"] == {"field": "name", "value": "lookup"}
        return httpx.Response(200, content=archive, headers={"content-type": "application/octet-stream"})

    fetcher = _install(monkeypatch, handler)
    payload = json.loads(await post_scripts_get(None, _request("name", "lookup")))
    await fetcher.aclose()

    assert payload["mimeType"] == "application/zip"
    assert base64.b64decode(payload["blob"]) == archive
    assert payload["size"] == len(archive)
    assert payload["files"] == ["script.yml"]


@pytest.mark.asyncio
async def test_performance_export_gunzips_json(monkeypatch):
    document = {"history": ["opened"], "workplan": {"tasks": 1}}
    compressed = gzip.compress(json.dumps(document).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/xsoar/public/v1/performance/incident/export/42"
        return httpx.Response(200, content=compressed, headers={"content-type": "application/gzip"})

    fetcher = _install(monkeypatch, handler)
    payload = json.loads(await get_performance_incident_export_by_incident_id(None, "42"))
    await fetcher.aclose()

    assert payload["history"] == document["history"]
    assert payload["workplan"] == document["workplan"]
    assert "blob" not in payload


@pytest.mark.asyncio
async def test_catalog_binary_body_is_a_clean_error():
    body = b"\xff\xfe\x00\x84"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})

    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(handler),
    )
    server = FastMCP.from_openapi(_CATALOG_SPEC, client)
    async with Client(server) as mcp_client:
        result = await mcp_client.call_tool_mcp("post_demo_get", {})
    await client.aclose()

    dumped = result.model_dump(by_alias=True)
    text = json.dumps(dumped)
    assert dumped["isError"] is True
    assert "'utf-8' codec" not in text
    assert binary_response_error("application/octet-stream", len(body)) in text


@pytest.mark.asyncio
async def test_send_gunzips_json_and_raw_keeps_the_gzip_bytes():
    document = {"reply": {"status": "SUCCESS"}}
    compressed = gzip.compress(json.dumps(document).encode())
    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=compressed)),
    )
    assert await client.request("POST", "/public_api/v1/demo/get", raw=True) == compressed
    assert await client.request("POST", "/public_api/v1/demo/get") == document
    await client.aclose()


@pytest.mark.asyncio
async def test_request_rejects_non_utf8_without_a_codec_error():
    body = bytes(range(128, 160))
    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, headers={"content-type": "application/pdf"})
        ),
    )
    with pytest.raises(PAPIResponseError, match="binary response") as caught:
        await client.request("POST", "/public_api/v1/demo/get")
    await client.aclose()
    assert "'utf-8' codec" not in str(caught.value)
    assert binary_response_error("application/pdf", len(body)) in str(caught.value)


def test_export_schemas_require_filter():
    spec = bundle_openapi_from_folders()
    for path in ("/public_api/v1/playbooks/get", "/public_api/v1/scripts/get"):
        operation = spec["paths"][path]["post"]
        assert operation["requestBody"]["required"] is True
        request_data = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]["request_data"]
        assert request_data["required"] == ["filter"]
        assert request_data["properties"]["filter"]["required"] == ["field", "value"]


@pytest.mark.asyncio
async def test_export_tools_require_filter_and_are_read_only():
    server = FastMCP("exports")
    discover_and_register_modules(server)
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("post_playbooks_get", "post_scripts_get"):
        tool = tools[name]
        assert "Side effects: none" in (tool.description or "")
        schema = tool.parameters or {}
        assert schema.get("required") == ["request_data"]
        request_data = schema["properties"]["request_data"]
        assert request_data["required"] == ["filter"]
        assert request_data["properties"]["filter"]["required"] == ["field", "value"]

    performance = tools["get_performance_incident_export_by_incident_id"]
    assert "Side effects: none" in (performance.description or "")
    assert "incident_id" in ((performance.parameters or {}).get("required") or [])


def test_route_map_excludes_binary_exports_when_writes_are_enabled(monkeypatch):
    monkeypatch.setenv("MCP_WRITE_TOOLS_ENABLED", "true")
    reload_config()
    try:
        for operation_id in BINARY_EXPORT_OPERATION_IDS:
            route = type(
                "Route",
                (),
                {
                    "operation_id": operation_id,
                    "description": "Side effects: none. This is a read-only Cortex API call",
                },
            )()
            assert openapi_route_map(route, MCPType.TOOL) is MCPType.EXCLUDE
        lookup = type(
            "Route",
            (),
            {
                "operation_id": "post_case_search",
                "description": "Side effects: none. This is a read-only Cortex API call",
            },
        )()
        assert openapi_route_map(lookup, MCPType.TOOL) is None
    finally:
        monkeypatch.delenv("MCP_WRITE_TOOLS_ENABLED", raising=False)
        reload_config()


@pytest.mark.asyncio
async def test_mapped_catalog_omits_the_handwritten_export_names():
    spec = bundle_openapi_from_folders()
    server = FastMCP.from_openapi(
        spec,
        httpx.AsyncClient(base_url="https://api.example.invalid"),
        route_map_fn=openapi_route_map,
    )
    names = {tool.name for tool in await server.list_tools()}
    assert BINARY_EXPORT_OPERATION_IDS.isdisjoint(names)

    registered = FastMCP("registered")
    ContentExportsModule(registered).register_tools()
    registered_names = {tool.name for tool in await registered.list_tools()}
    assert registered_names == set(BINARY_EXPORT_OPERATION_IDS)


def test_gzip_magic_and_decoded_gzip_header_both_parse_json():
    document = {"history": ["opened"]}
    encoded = json.dumps(document).encode()
    assert interpret_export_body(gzip.compress(encoded), content_encoding="gzip") == document
    assert interpret_export_body(encoded, content_type="application/octet-stream", content_encoding="gzip") == document


def test_oversized_archive_is_rejected_without_the_blob(monkeypatch):
    archive = _zip_bytes("playbook.yml", "name: demo\n")
    monkeypatch.setattr("usecase.builtin_components.content_exports._MAX_EXPORT_BYTES", len(archive) - 1)
    with pytest.raises(ValueError, match="exceeds"):
        interpret_export_body(archive, content_type="application/zip")
    assert _MAX_EXPORT_BYTES > len(archive)


@pytest.mark.asyncio
async def test_empty_incident_id_does_not_call_the_tenant(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected call {request.url}")

    fetcher = _install(monkeypatch, handler)
    payload = json.loads(await get_performance_incident_export_by_incident_id(None, " "))
    await fetcher.aclose()
    assert payload["success"] == "false"
    assert "empty" in payload["error"]
