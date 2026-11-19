import json

import httpx2 as httpx
import pytest
from fastmcp import Client, FastMCP

from pkg.client import PAPIClient
from pkg.input_validation import JsonSchemaInputMiddleware
from pkg.response_envelope import ResponseEnvelopeMiddleware
from pkg.util import bundle_openapi_from_folders

_ENVELOPE_CALLS = (
    (
        "post_mth_child_get_comments",
        {"request_data": {"xsoar_source_id": "src-1"}},
        "/public_api/v1/mth/child/get_comments",
    ),
    (
        "post_mth_child_get_reports_by_statuses",
        {"request_data": {"report_statuses": ["New"]}},
        "/public_api/v1/mth/child/get_reports_by_statuses",
    ),
    (
        "post_mth_child_get_reports_by_source_id",
        {"request_data": {"xsoar_source_ids": ["src-1"]}},
        "/public_api/v1/mth/child/get_reports_by_source_id",
    ),
    (
        "post_mth_child_get_reports_by_incident_id",
        {"request_data": {"incident_ids": ["1"]}},
        "/public_api/v1/mth/child/get_reports_by_incident_id",
    ),
    (
        "post_cloud_onboarding_get_identifiers",
        {"request_data": {"instance_id": "inst-1", "cloud_provider": "GCP"}},
        "/public_api/v1/cloud_onboarding/get_identifiers",
    ),
)


def _client(handler) -> PAPIClient:
    return PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(handler),
    )


def _json_body(request: httpx.Request):
    raw = bytes(request.content)
    if not raw:
        return None
    return json.loads(raw)


@pytest.fixture(scope="module")
def catalog_server():
    calls: list[httpx.Request] = []
    controls = [{"id": index, "name": f"control-{index}"} for index in range(21679)]
    datasets = [{"dataset_name": f"dataset-{index}"} for index in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path.rstrip("/")
        if path.endswith("/compliance/get_controls"):
            return httpx.Response(200, json={"reply": {"data": controls, "result_count": len(controls)}})
        if path.endswith("/xql/get_datasets"):
            return httpx.Response(200, json={"reply": datasets})
        return httpx.Response(200, json={"reply": {"ok": True}})

    client = _client(handler)
    server = FastMCP.from_openapi(bundle_openapi_from_folders(), client, strict_input_validation=True)
    server.add_middleware(ResponseEnvelopeMiddleware())
    server.add_middleware(JsonSchemaInputMiddleware())
    return server, calls


def _matching(calls: list[httpx.Request], path: str) -> httpx.Request:
    matches = [call for call in calls if call.url.path.rstrip("/") == path]
    assert matches, path
    return matches[-1]


async def _call(client: Client, name: str, arguments: dict):
    result = await client.call_tool_mcp(name, arguments)
    assert result.is_error is False, result.content[0].text if result.content else result
    return result


@pytest.mark.asyncio
async def test_envelope_tools_send_only_request_data(catalog_server):
    server, calls = catalog_server
    async with Client(server) as client:
        for name, arguments, path in _ENVELOPE_CALLS:
            before = len(calls)
            await _call(client, name, arguments)
            request = _matching(calls[before:], path)
            assert _json_body(request) == arguments
            assert list(_json_body(request)) == ["request_data"]


@pytest.mark.asyncio
async def test_empty_calls_send_an_empty_request_data_object(catalog_server):
    server, calls = catalog_server
    async with Client(server) as client:
        for name, path in (
            (
                "post_compliance_get_control_categories_and_subcategories",
                "/public_api/v1/compliance/get_control_categories_and_subcategories",
            ),
            (
                "get_assessment_profile_results",
                "/public_api/v1/compliance/get_assessment_results",
            ),
        ):
            before = len(calls)
            await _call(client, name, {})
            request = _matching(calls[before:], path)
            assert _json_body(request) == {"request_data": {}}


@pytest.mark.asyncio
async def test_empty_data_security_post_sends_json_object(catalog_server):
    server, calls = catalog_server
    async with Client(server) as client:
        before = len(calls)
        await _call(client, "post_data_security_objects_fields", {})
    request = _matching(calls[before:], "/public_api/v1/data-security/objects/fields")
    assert _json_body(request) == {}
    assert request.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_broker_image_type_is_a_query_parameter(catalog_server):
    server, calls = catalog_server
    async with Client(server) as client:
        before = len(calls)
        await _call(client, "get_brokers_images", {"type": "OVA"})
    request = _matching(calls[before:], "/public_api/v1/brokers/images")
    assert request.method == "GET"
    assert request.url.params["type"] == "OVA"
    assert bytes(request.content) == b""


@pytest.mark.asyncio
async def test_controls_page_is_sliced_and_datasets_have_a_default_page(catalog_server):
    server, calls = catalog_server
    async with Client(server) as client:
        controls = await _call(
            client,
            "post_compliance_get_controls",
            {"request_data": {"search_from": 0, "search_to": 1}},
        )
        datasets = await _call(client, "post_xql_get_datasets", {})

    parsed = json.loads(controls.content[0].text)
    assert len(parsed["reply"]["data"]) == 1
    assert parsed["reply"]["data"][0]["id"] == 0
    assert parsed["reply"]["result_count"] == 21679
    assert parsed["truncated"] is True
    assert parsed["returned"] == 1
    assert parsed["total"] == 21679
    assert len(controls.content[0].text.encode("utf-8")) < 256 * 1024

    parsed_datasets = json.loads(datasets.content[0].text)
    assert len(parsed_datasets["reply"]) == 100
    assert parsed_datasets["truncated"] is True
    assert parsed_datasets["returned"] == 100
    assert parsed_datasets["total"] == 150


@pytest.mark.asyncio
async def test_empty_write_requests_gain_a_json_body(monkeypatch):
    async def _no_wait(self, attempt: int) -> None:
        return None

    monkeypatch.setattr(PAPIClient, "_pause_before_retry", _no_wait)
    seen: list[tuple[str, bytes, str | None]] = []
    retries = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, bytes(request.content), request.headers.get("content-type")))
        if request.url.path.endswith("/retry"):
            retries["count"] += 1
            if retries["count"] == 1:
                return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    empty = client.build_request("POST", "/public_api/v1/data-security/objects/fields")
    kept = client.build_request("POST", "/public_api/v1/case/search", json={"request_data": {"filters": []}})
    multipart = client.build_request(
        "POST",
        "/public_api/v1/scripts/insert",
        headers={"Content-Type": "multipart/form-data; boundary=abc"},
        content=b"",
    )
    lookup = client.build_request("GET", "/public_api/v1/brokers/images/", params={"type": "OVA"})
    retry = client.build_request("PUT", "/public_api/v1/retry")

    await client.send(empty)
    await client.send(kept)
    await client.send(multipart)
    await client.send(lookup)
    await client.send(retry)
    await client.aclose()

    assert seen[0] == ("POST", b"{}", "application/json")
    assert seen[1][0] == "POST"
    assert json.loads(seen[1][1]) == {"request_data": {"filters": []}}
    assert seen[1][2].startswith("application/json")
    assert seen[2] == ("POST", b"", "multipart/form-data; boundary=abc")
    assert seen[3][0] == "GET"
    assert seen[3][1] == b""
    assert seen[4] == ("PUT", b"{}", "application/json")
    assert seen[5] == ("PUT", b"{}", "application/json")


@pytest.mark.asyncio
async def test_request_and_download_defaults_do_not_stay_on_the_client():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    initial = httpx.Headers(client.headers)

    await client.stream("POST", "/public_api/v1/mcp/download/", data="{}")
    assert client.headers == initial
    await client.request(
        "POST", "/public_api/v1/case/search", json={"request_data": {}}, headers={"Accept": "application/x-test"}
    )
    assert client.headers == initial
    await client.request("POST", "/public_api/v1/scripts/insert", files={"file": ("a.zip", b"PK", "application/zip")})
    assert client.headers == initial
    await client.send(client.build_request("GET", "/public_api/v1/healthcheck"))
    await client.aclose()

    download, search, upload, catalog = seen
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["x-is-mcp"] == "true"
    assert search.headers["content-type"] == "application/json"
    assert search.headers["x-is-mcp"] == "true"
    assert search.headers["accept"] == "application/x-test"
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert upload.headers["x-is-mcp"] == "true"
    assert "content-type" not in catalog.headers
    assert catalog.headers["accept"] == initial["accept"]
    assert catalog.headers["x-is-mcp"] == "true"
