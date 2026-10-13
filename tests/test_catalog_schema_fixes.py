"""Schema defaults from issue 30. No tenant credentials are used."""

import json
from dataclasses import dataclass, field

import httpx2 as httpx
import pytest
from fastmcp import Client, FastMCP

from pkg.client import PAPIClient
from pkg.response_envelope import ResponseEnvelopeMiddleware
from pkg.tool_presentation import annotate_openapi_component
from pkg.util import bundle_openapi_from_folders

GET_INSTANCES = "/public_api/v1/cloud_onboarding/get_instances"
LIST_APPLICATIONS = "/platform/integration/v1/external-application"
APPLICATION_BY_ID = "/platform/integration/v1/external-application/{application_type}/id/{application_id}"
SORT_FIELDS = [
    "STATUS",
    "PROVIDER",
    "INSTANCE_NAME",
    "SCOPE",
    "SCAN_MODE",
    "CREATION_TIME",
    "OUTPOST_ID",
    "AUTHENTICATION_METHOD",
    "ID",
]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _operation(spec: dict, path: str, method: str) -> dict:
    return spec["paths"][path][method]


def _parameter(operation: dict, name: str) -> dict:
    return next(parameter for parameter in operation["parameters"] if parameter["name"] == name)


@dataclass
class _Capture:
    requests: list[httpx.Request] = field(default_factory=list)
    download_body: bytes = b""
    download_content_type: str = "text/plain"


def _handler(capture: _Capture):
    def handler(request: httpx.Request) -> httpx.Response:
        capture.requests.append(request)
        path = request.url.path
        if path.endswith("/lists/download/list-1"):
            return httpx.Response(
                200,
                content=capture.download_body,
                headers={"content-type": capture.download_content_type},
            )
        if path == LIST_APPLICATIONS:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "application_id": "e94fbc98-3053-6506-876b-f809f344f118",
                            "application_type": "slack",
                            "name": "alerts",
                        }
                    ]
                },
            )
        if "/external-application/" in path and "/id/" in path:
            return httpx.Response(
                200,
                json={"data": {"application_id": 7, "application_type": "slack", "name": "alerts"}},
            )
        return httpx.Response(200, json={"reply": {"DATA": []}})

    return handler


@pytest.fixture(scope="module")
def bundled_spec():
    spec = bundle_openapi_from_folders()
    assert spec is not None
    return spec


@pytest.fixture(scope="module")
def catalog(bundled_spec):
    capture = _Capture()
    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(_handler(capture)),
    )
    server = FastMCP.from_openapi(
        bundled_spec,
        client,
        mcp_component_fn=annotate_openapi_component,
    )
    server.add_middleware(ResponseEnvelopeMiddleware())
    return server, capture


def _request_json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def _schema_dict(schema) -> dict:
    if schema is None:
        return {}
    if isinstance(schema, dict):
        return schema
    return schema.model_dump(by_alias=True, exclude_none=True)


def test_cloud_instance_sort_schema_lists_supported_fields(bundled_spec):
    operation = _operation(bundled_spec, GET_INSTANCES, "post")
    request_data = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]["request_data"]
    sort_schema = request_data["properties"]["filter_data"]["properties"]["sort"]
    field_schema = sort_schema["items"]["properties"]["FIELD"]

    assert field_schema["enum"] == SORT_FIELDS
    assert "MODIFIED_AT" not in field_schema["enum"]
    assert sort_schema["default"] == [{"FIELD": "STATUS", "ORDER": "ASC"}]
    assert "Cannot find mapping for MODIFIED_AT" in operation["description"]


def test_external_application_type_is_shared_and_includes_slack(bundled_spec):
    application_type = bundled_spec["components"]["schemas"]["ExternalApplicationType"]
    assert application_type["enum"] == ["syslog", "webhook", "splunk", "aws_sqs", "aws_s3", "slack"]
    shared = {"$ref": "#/components/schemas/ExternalApplicationType"}

    listed = _operation(bundled_spec, LIST_APPLICATIONS, "get")
    item = listed["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]["items"]
    assert item["properties"]["application_type"] == shared
    id_schema = item["properties"]["application_id"]
    assert {"type": "integer", "format": "int64"} in id_schema["anyOf"]
    assert {"type": "string"} in id_schema["anyOf"]

    for method in ("get", "delete"):
        operation = _operation(bundled_spec, APPLICATION_BY_ID, method)
        assert _parameter(operation, "application_type")["schema"] == shared
        identifier = _parameter(operation, "application_id")["schema"]
        assert identifier["type"] == "integer"
        assert identifier["format"] == "int64"

    created = _operation(bundled_spec, LIST_APPLICATIONS, "post")
    created_type = created["requestBody"]["content"]["application/json"]["schema"]["allOf"][0]["properties"][
        "request_data"
    ]["properties"]["application_type"]
    assert created_type == shared


@pytest.mark.asyncio
async def test_get_instances_default_call_sends_a_supported_sort(catalog):
    server, capture = catalog
    capture.requests.clear()
    async with Client(server, mode="2026-07-28") as client:
        listed = await client.list_tools()
        result = await client.call_tool_mcp("post_cloud_onboarding_get_instances", {"request_data": {}})

    tool = next(item for item in listed if item.name == "post_cloud_onboarding_get_instances")
    field_enums = [
        node["enum"]
        for node in _walk(_schema_dict(tool.input_schema))
        if isinstance(node.get("enum"), list) and "STATUS" in node["enum"] and "PROVIDER" in node["enum"]
    ]
    assert field_enums == [SORT_FIELDS]
    assert result.is_error is False
    body = _request_json(capture.requests[-1])
    sort = body["request_data"]["filter_data"]["sort"]
    assert sort == [{"FIELD": "STATUS", "ORDER": "ASC"}]
    assert sort[0]["FIELD"] in SORT_FIELDS

    capture.requests.clear()
    async with Client(server, mode="2026-07-28") as client:
        kept = await client.call_tool_mcp(
            "post_cloud_onboarding_get_instances",
            {
                "request_data": {
                    "filter_data": {"sort": [{"FIELD": "PROVIDER", "ORDER": "DESC"}], "paging": {"from": 0}}
                }
            },
        )
    assert kept.is_error is False
    kept_body = _request_json(capture.requests[-1])
    assert kept_body["request_data"]["filter_data"]["sort"] == [{"FIELD": "PROVIDER", "ORDER": "DESC"}]
    assert kept_body["request_data"]["filter_data"]["paging"] == {"from": 0}


@pytest.mark.asyncio
async def test_external_application_by_id_accepts_slack_and_an_integer_id(catalog):
    server, capture = catalog
    capture.requests.clear()
    async with Client(server, mode="2026-07-28") as client:
        listed = await client.list_tools()
        looked_up = await client.call_tool_mcp(
            "get_integration_v1_external_application_by_applic_9b2917",
            {"application_type": "slack", "application_id": 7},
        )
        applications = await client.call_tool_mcp("get_integration_v1_external_application", {})

    tool = next(item for item in listed if item.name == "get_integration_v1_external_application_by_applic_9b2917")
    properties = _schema_dict(tool.input_schema)["properties"]
    assert "slack" in properties["application_type"]["enum"]
    assert properties["application_id"]["type"] == "integer"
    assert looked_up.is_error is False
    by_id = next(request for request in capture.requests if "/id/" in request.url.path)
    assert by_id.url.path.endswith("/external-application/slack/id/7")
    assert applications.is_error is False
    listed_body = applications.structured_content
    assert listed_body["data"][0]["application_type"] == "slack"
    assert listed_body["data"][0]["application_id"] == "e94fbc98-3053-6506-876b-f809f344f118"


@pytest.mark.asyncio
async def test_list_download_empty_and_plain_text_return_structured_content(catalog):
    server, capture = catalog
    async with Client(server, mode="2026-07-28") as client:
        listed = await client.list_tools()
        download = next(item for item in listed if item.name == "get_lists_download_by_list_id")
        assert download.output_schema is None

        capture.download_body = b""
        capture.download_content_type = "text/plain"
        empty = await client.call_tool_mcp("get_lists_download_by_list_id", {"list_id": "list-1"})

        capture.download_body = b"alpha,beta"
        capture.download_content_type = "text/plain"
        plain = await client.call_tool_mcp("get_lists_download_by_list_id", {"list_id": "list-1"})

    assert empty.is_error is False
    assert empty.structured_content["content"] == ""
    assert plain.is_error is False
    assert plain.structured_content["content"] == "alpha,beta"
    assert "content" in json.loads(plain.content[0].text)
