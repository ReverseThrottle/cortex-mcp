import gzip
import inspect
import json
import logging
import os
import re
from pathlib import Path

import httpx2 as httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.elicitation import AcceptedElicitation
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.providers.openapi.routing import MCPType
from mcp.types import CallToolRequestParams

from config.config import reload_config
from entities.exceptions import PAPIResponseError
from main import initialize_mcp_server, openapi_route_map
from pkg.client import PAPIClient
from pkg.setup_logging import configure_library_logging
from pkg.tool_classification import SIDE_EFFECT_EXTENSION
from pkg.tool_presentation import (
    annotate_openapi_component,
    annotations_for_side_effect,
)
from pkg.util import bundle_openapi_from_folders
from pkg.write_confirmation import (
    WriteConfirmation,
    WriteConfirmationMiddleware,
    changes_tenant_state,
)
from usecase.builtin_components.content_exports import BINARY_EXPORT_OPERATION_IDS
from usecase.custom_components import xql_query as xql_query_module
from usecase.custom_components.xql_query import decode_xql_stream, run_xql_query
from usecase.fetcher import get_fetcher
from usecase.module_util import discover_and_register_modules

CREDENTIAL_PARAMETERS = {
    "authorization",
    "x-xdr-auth-id",
    "x_xdr_auth_id",
    "x-xdr-nonce",
    "x-xdr-timestamp",
    "x_xdr_nonce",
    "x_xdr_timestamp",
}

REQUIRED_PATHS = (
    "POST /public_api/v1/incidents/get_incidents",
    "POST /public_api/v1/alerts/get_alerts",
    "POST /public_api/v1/case/update/{case_id}",
    "POST /public_api/v1/distributions/delete",
    "POST /public_api/v1/xql/start_xql_query",
    "POST /public_api/v1/xql/get_query_results",
    "POST /public_api/v1/xql/get_quota",
    "POST /public_api/v1/endpoints/get_endpoints",
    "GET /public_api/appsec/v1/package_explorer/packages/{name}/versions/{version}",
)

READ_ONLY_PYTHON_TOOLS = {
    "get_cases",
    "get_issues",
    "get_performance_incident_export_by_incident_id",
    "post_playbooks_get",
    "post_scripts_get",
    "post_xql_get_query_results_stream",
}

WRITE_PYTHON_TOOLS = {
    "update_case",
    "run_xql_query",
    "insert_script",
    "insert_playbook",
    "post_playbook_save_yaml",
    "post_entry_upload_by_incident_id",
    "post_incident_upload_by_incident_id",
    "post_inv_playbook_task_complete",
    "put_settings_credentials",
}

ISOLATE_PYTHON_TOOLS = {
    "isolate_endpoint",
    "unisolate_endpoint",
}

ISSUE_26_TOOLS = (
    "post_endpoints_file_retrieval",
    "post_brokers_by_device_id_applets_wec_wef_cert",
    "post_settings_credentials",
    "get_engines_download_by_id",
    "post_distributions_get_dist_url",
)

SECRET_CATALOG_TOOLS = ISSUE_26_TOOLS[1:] + ("get_brokers_images",)

READ_LOOKUP_TOOLS = (
    "post_cloud_consumption_v1_per_asset_type",
    "post_cloud_consumption_v1_over_time",
    "post_cloud_consumption_v1_details",
    "post_actions_file_retrieval_details",
)

SIDE_EFFECT_CLASSES = {"read", "action", "write", "secret"}

PYTHON_ONLY_PATHS = (
    "/xsoar/public/v1/playbook/save/yaml",
    "/xsoar/public/v1/entry/upload/{incident_id}",
    "/xsoar/public/v1/incident/upload/{incident_id}",
    "/xsoar/public/v1/inv-playbook/task/complete",
    "/public_api/v1/xql/get_query_results_stream",
)


def _norm(method: str, path: str) -> tuple[str, str]:
    cleaned = path.rstrip("/") or "/"
    cleaned = re.sub(r"\{[^}]+\}", "{}", cleaned)
    return method.upper(), cleaned


def _operation(spec: dict, path: str, method: str = "post") -> dict:
    return spec["paths"][path][method]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


@pytest.fixture(scope="module")
def bundled_spec():
    spec = bundle_openapi_from_folders()
    assert spec is not None
    return spec


@pytest.fixture(scope="module")
def openapi_server(bundled_spec):
    return FastMCP.from_openapi(
        bundled_spec,
        httpx.AsyncClient(base_url="https://api.example.invalid"),
    )


@pytest.fixture
def feature_flags(monkeypatch):
    previous = {
        "MCP_WRITE_TOOLS_ENABLED": os.environ.get("MCP_WRITE_TOOLS_ENABLED"),
        "MCP_SECRET_TOOLS_ENABLED": os.environ.get("MCP_SECRET_TOOLS_ENABLED"),
        "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED": os.environ.get("MCP_ISOLATE_ENDPOINT_TOOL_ENABLED"),
    }

    def apply(*, write: bool, isolate: bool, secret: bool = False):
        monkeypatch.setenv("MCP_WRITE_TOOLS_ENABLED", "true" if write else "false")
        monkeypatch.setenv("MCP_SECRET_TOOLS_ENABLED", "true" if secret else "false")
        monkeypatch.setenv("MCP_ISOLATE_ENDPOINT_TOOL_ENABLED", "true" if isolate else "false")
        return reload_config()

    yield apply

    for name, value in previous.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reload_config()


async def _tool_map(server: FastMCP) -> dict:
    return {tool.name: tool for tool in await server.list_tools()}


@pytest.mark.asyncio
async def test_catalog_covers_documented_operations_without_credential_parameters(bundled_spec, openapi_server):
    tools = await _tool_map(openapi_server)
    assert len(tools) == 506

    descriptions = {name: (tool.description or "") for name, tool in tools.items()}
    missing_side_effects = [name for name, description in descriptions.items() if "Side effects:" not in description]
    assert missing_side_effects == []

    long_names = [name for name in tools if len(name) > 56 or "__" in name]
    assert long_names == []
    assert len(tools) == len(set(tools))

    leaked = []
    for name, tool in tools.items():
        properties = (tool.parameters or {}).get("properties") or {}
        for key in properties:
            if key.lower() in CREDENTIAL_PARAMETERS:
                leaked.append((name, key))
    assert leaked == []

    blob = "\n".join(descriptions.values())
    for path in REQUIRED_PATHS:
        assert path in blob

    xsoar = [name for name, description in descriptions.items() if "/xsoar/public/v1/" in description]
    assert xsoar

    asset_posts = [
        name
        for name, description in descriptions.items()
        if "(POST /public_api/v1/assets/)" in description or "(POST /public_api/v1/assets)." in description
    ]
    assert asset_posts == ["get_assets"]
    assert "get_filtered_endpoints" in tools
    assert any("POST /public_api/v1/endpoints/get_endpoints" in description for description in descriptions.values())
    edit_profile = tools["post_compliance_edit_assessment_profile"]
    edit_properties = (edit_profile.parameters or {}).get("properties") or {}
    assert "request_data" in edit_properties
    assert "" not in edit_properties


def test_catalog_schemas_match_the_documented_calls(bundled_spec):
    assert not any("?" in path for path in bundled_spec["paths"])
    for path in PYTHON_ONLY_PATHS:
        assert path not in bundled_spec["paths"]
    credentials = bundled_spec["paths"]["/xsoar/public/v1/settings/credentials"]
    assert "put" not in credentials
    assert "post" in credentials

    list_types = [node["type"] for node in _walk(bundled_spec) if isinstance(node.get("type"), list)]
    assert list_types == []

    clipped = [
        node["description"]
        for node in _walk(bundled_spec)
        if isinstance(node.get("description"), str)
        and node["description"].endswith("...")
        and 300 <= len(node["description"]) <= 400
    ]
    assert clipped == []

    report_status = _operation(bundled_spec, "/public_api/v1/mth/child/report/update/status")
    rcs = _operation(bundled_spec, "/public_api/v1/remediation_confirmation_scanning/requests/get_or_create/")
    for operation in (report_status, rcs):
        assert "Side effects: this operation changes" in operation["description"]
        assert "Side effects: none" not in operation["description"]

    quarantine_status = _operation(bundled_spec, "/public_api/v1/quarantine/status")
    assert quarantine_status["description"].startswith("Side effects: none.")

    scan_filters = _operation(bundled_spec, "/public_api/v1/endpoints/scan")["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["request_data"]["properties"]["filters"]
    assert any(branch.get("type") == "array" for branch in scan_filters["anyOf"])
    assert any(branch.get("enum") == ["all"] for branch in scan_filters["anyOf"])

    quarantine = _operation(bundled_spec, "/public_api/v1/endpoints/quarantine")
    file_hash = quarantine["requestBody"]["content"]["application/json"]["schema"]["properties"]["request_data"][
        "properties"
    ]["file_hash"]
    assert "SHA256" in file_hash["description"]
    assert "Case ID" not in file_hash["description"]

    alert_value = _operation(bundled_spec, "/public_api/v1/alerts/get_alerts")["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["request_data"]["properties"]["filters"]["items"]["properties"]["value"]
    assert any(branch.get("type") == "array" for branch in alert_value["anyOf"])
    assert alert_value.get("type") != "string"

    endpoints = _operation(bundled_spec, "/public_api/v1/endpoints/get_endpoints")["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["request_data"]["properties"]
    assert {"filters", "search_from", "search_to"} <= set(endpoints)

    def filter_value(path: str) -> dict:
        return _operation(bundled_spec, path)["requestBody"]["content"]["application/json"]["schema"]["properties"][
            "request_data"
        ]["properties"]["filters"]["items"]["properties"]["value"]

    for path in ("/public_api/v1/widgets/get", "/public_api/v1/assets/get_business_units"):
        value = filter_value(path)
        assert value.get("type") != "string"
        assert any(branch.get("type") == "array" for branch in value["anyOf"])
    for path in (
        "/public_api/v1/legacy_exceptions/fetch",
        "/public_api/v1/disable_injection_prevention_rules/fetch",
    ):
        value = filter_value(path)
        assert value.get("type") != "string"
        assert any(branch.get("type") in {"number", "integer"} for branch in value["anyOf"])


# Documented bodies that do not use the Cortex request_data envelope.
REQUEST_DATA_ALLOW_LIST = frozenset(
    {
        "/public_api/v1/brokers/{device_id}/applets/{applet_name}/config/",
        "/public_api/v1/brokers/{device_id}/applets/{applet_name}/activate/",
        "/public_api/v1/brokers/{device_id}/applets/{applet_name}/deactivate/",
        "/public_api/v1/brokers/{device_id}/applets/wec/wef_cert/",
        "/public_api/v1/brokers/{device_id}/",
        "/public_api/v1/entries/get",
        "/public_api/v1/entries/insert",
        "/public_api/v1/cwp/policies",
        "/public_api/v1/cwp/registry_onboarding/instances",
        "/public_api/v1/distributions/get_versions",
        "/public_api/v1/legacy_exceptions/get_modules",
        "/public_api/v1/get_risky_users",
        "/public_api/v1/get_risky_hosts",
        "/public_api/v1/xql/get_quota",
        "/public_api/v1/dataset/get_created_datasets",
        "/public_api/v1/data-security/data-patterns",
        "/public_api/v1/data-security/objects/fields",
        "/public_api/v1/data-security/objects/files",
        "/public_api/v1/rule",
        "/public_api/v1/rule/search",
        "/public_api/v1/mth/child/report/update/assign",
        "/public_api/v1/mth/child/add_comment",
        "/public_api/v1/mth/child/report/update/status",
        "/public_api/v1/policy",
        "/public_api/v1/policy/search",
    }
)

ENVELOPE_FIELDS = {
    "/public_api/v1/mth/child/get_comments": "xsoar_source_id",
    "/public_api/v1/mth/child/get_reports_by_statuses": "report_statuses",
    "/public_api/v1/mth/child/get_reports_by_source_id": "xsoar_source_ids",
    "/public_api/v1/mth/child/get_reports_by_incident_id": "incident_ids",
    "/public_api/v1/cloud_onboarding/get_identifiers": "instance_id",
}


def _resolve_schema(spec: dict, schema: dict, seen: set[str] | None = None) -> dict:
    if not isinstance(schema, dict):
        return {}
    seen = set() if seen is None else set(seen)
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {}
        seen.add(ref)
        node = spec
        for part in ref[2:].split("/"):
            node = node[part]
        return _resolve_schema(spec, node, seen)
    if "allOf" in schema:
        merged: dict = {"properties": {}, "required": []}
        for part in schema["allOf"]:
            piece = _resolve_schema(spec, part, seen)
            merged["properties"].update(piece.get("properties") or {})
            for name in piece.get("required") or []:
                if name not in merged["required"]:
                    merged["required"].append(name)
        for key, value in schema.items():
            if key == "allOf":
                continue
            if key == "properties" and isinstance(value, dict):
                merged["properties"].update(value)
            elif key == "required" and isinstance(value, list):
                for name in value:
                    if name not in merged["required"]:
                        merged["required"].append(name)
            else:
                merged[key] = value
        return merged
    return schema


def _post_schema(spec: dict, path: str) -> dict:
    operation = spec["paths"][path]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    return _resolve_schema(spec, schema)


def test_public_api_posts_use_request_data_and_gets_have_no_body(bundled_spec):
    gets_with_body = [
        f"GET {path}"
        for path, methods in bundled_spec["paths"].items()
        for method, operation in methods.items()
        if method.lower() == "get" and isinstance(operation, dict) and "requestBody" in operation
    ]
    assert gets_with_body == []

    missing = []
    for path, methods in bundled_spec["paths"].items():
        if not path.startswith("/public_api/v1/"):
            continue
        operation = methods.get("post")
        if not isinstance(operation, dict) or "requestBody" not in operation:
            continue
        properties = _post_schema(bundled_spec, path).get("properties") or {}
        if "request_data" not in properties and path not in REQUEST_DATA_ALLOW_LIST:
            missing.append(path)
    assert missing == []

    for path in REQUEST_DATA_ALLOW_LIST:
        assert "request_data" not in (_post_schema(bundled_spec, path).get("properties") or {})

    for path, field in ENVELOPE_FIELDS.items():
        schema = _post_schema(bundled_spec, path)
        assert schema["required"] == ["request_data"]
        assert list(schema["properties"]) == ["request_data"]
        assert field in schema["properties"]["request_data"]["properties"]

    categories = _post_schema(bundled_spec, "/public_api/v1/compliance/get_control_categories_and_subcategories")
    request_data = categories["properties"]["request_data"]
    assert categories["required"] == ["request_data"]
    assert request_data["default"] == {}
    assert request_data["additionalProperties"] is False
    assert request_data["maxProperties"] == 0

    assessment = _post_schema(bundled_spec, "/public_api/v1/compliance/get_assessment_results/")
    assert "request_data" in assessment["required"]
    assert assessment["properties"]["request_data"]["default"] == {}

    edit_profile = _post_schema(bundled_spec, "/public_api/v1/compliance/edit_assessment_profile")
    assert edit_profile["required"] == ["request_data"]
    assert list(edit_profile["properties"]) == ["request_data"]
    assert "id" in edit_profile["properties"]["request_data"]["properties"]

    images = bundled_spec["paths"]["/public_api/v1/brokers/images/"]["get"]
    assert "requestBody" not in images
    image_type = images["parameters"][0]
    assert image_type["name"] == "type"
    assert image_type["in"] == "query"
    assert image_type["required"] is True
    assert image_type["schema"]["enum"] == ["OVA", "QCOW2", "VHD", "VHD_AZURE", "VMDK"]


def test_existing_helpers_are_not_duplicated_in_the_catalog(bundled_spec):
    present = set()
    for path, methods in bundled_spec["paths"].items():
        for method in methods:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                present.add(_norm(method, path))

    already_implemented = {
        ("POST", "/public_api/v1/case/search"),
        ("POST", "/public_api/v1/issue/search"),
        ("POST", "/public_api/v1/endpoints/isolate"),
        ("POST", "/public_api/v1/endpoints/unisolate"),
        ("POST", "/public_api/v1/scripts/insert"),
        ("POST", "/public_api/v1/playbooks/insert"),
    }
    assert present.isdisjoint(already_implemented)
    assert ("POST", "/public_api/v1/endpoints/get_endpoint") in present
    assert ("POST", "/public_api/v1/endpoints/get_endpoints") in present
    assert ("POST", "/public_api/v1/system/get_tenant_info") in present
    assert ("GET", "/public_api/v1/assets/{}") in present


@pytest.mark.asyncio
async def test_python_tools_follow_the_write_and_isolate_flags(openapi_server, feature_flags):
    feature_flags(write=False, isolate=False)
    read_only = FastMCP("read-only")
    discover_and_register_modules(read_only)
    read_tools = await _tool_map(read_only)
    read_names = set(read_tools)
    assert READ_ONLY_PYTHON_TOOLS <= read_names
    assert read_names.isdisjoint(WRITE_PYTHON_TOOLS)
    assert read_names.isdisjoint(ISOLATE_PYTHON_TOOLS)
    stream = read_tools["post_xql_get_query_results_stream"]
    assert "Side effects: none" in (stream.description or "")

    feature_flags(write=True, isolate=False)
    writes = FastMCP("writes")
    discover_and_register_modules(writes)
    write_tools = await _tool_map(writes)
    write_names = set(write_tools)
    assert WRITE_PYTHON_TOOLS <= write_names
    assert write_names.isdisjoint(ISOLATE_PYTHON_TOOLS)
    assert "Side effects: this operation changes" in (write_tools["update_case"].description or "")
    assert "Side effects: none" in (write_tools["get_cases"].description or "")

    feature_flags(write=True, isolate=True)
    both = FastMCP("both")
    discover_and_register_modules(both)
    both_names = set(await _tool_map(both))
    catalog_names = set(await _tool_map(openapi_server))
    assert WRITE_PYTHON_TOOLS | ISOLATE_PYTHON_TOOLS | READ_ONLY_PYTHON_TOOLS <= both_names
    # The unmapped catalog still lists the zip and gzip exports. openapi_route_map
    # drops those operation ids so the handwritten tools are the only copies.
    assert both_names.isdisjoint(catalog_names - BINARY_EXPORT_OPERATION_IDS)
    assert BINARY_EXPORT_OPERATION_IDS <= both_names
    assert BINARY_EXPORT_OPERATION_IDS <= catalog_names
    isolate = (await _tool_map(both))["isolate_endpoint"]
    assert "Side effects:" in (isolate.description or "")


def _route(kind: str | None):
    extensions = {} if kind is None else {SIDE_EFFECT_EXTENSION: kind}
    return type(
        "Route",
        (),
        {
            "extensions": extensions,
            "operation_id": None,
            "description": "Side effects: none. This is a read-only Cortex API call",
        },
    )()


def _annotated_catalog(spec: dict):
    return FastMCP.from_openapi(
        spec,
        httpx.AsyncClient(base_url="https://api.example.invalid"),
        route_map_fn=openapi_route_map,
        mcp_component_fn=annotate_openapi_component,
    )


def test_every_catalog_operation_declares_a_side_effect(bundled_spec):
    missing = []
    for path, methods in bundled_spec["paths"].items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                continue
            if operation.get(SIDE_EFFECT_EXTENSION) not in SIDE_EFFECT_CLASSES:
                missing.append(f"{method.upper()} {path}")
    assert missing == []


def test_unclassified_tools_fail_closed():
    annotations = annotations_for_side_effect(None)
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is True
    assert annotations.idempotent_hint is False


def test_openapi_route_map_hides_non_read_catalog_tools(feature_flags):
    feature_flags(write=False, isolate=False, secret=False)
    assert openapi_route_map(_route("read"), MCPType.TOOL) is None
    assert openapi_route_map(_route("action"), MCPType.TOOL) is MCPType.EXCLUDE
    assert openapi_route_map(_route("write"), MCPType.TOOL) is MCPType.EXCLUDE
    assert openapi_route_map(_route("secret"), MCPType.TOOL) is MCPType.EXCLUDE
    assert openapi_route_map(_route(None), MCPType.TOOL) is MCPType.EXCLUDE

    feature_flags(write=True, isolate=False, secret=False)
    assert openapi_route_map(_route("action"), MCPType.TOOL) is None
    assert openapi_route_map(_route("write"), MCPType.TOOL) is None
    assert openapi_route_map(_route("secret"), MCPType.TOOL) is MCPType.EXCLUDE
    assert openapi_route_map(_route(None), MCPType.TOOL) is None

    feature_flags(write=False, isolate=False, secret=True)
    assert openapi_route_map(_route("secret"), MCPType.TOOL) is MCPType.EXCLUDE

    feature_flags(write=True, isolate=False, secret=True)
    assert openapi_route_map(_route("secret"), MCPType.TOOL) is None
    text = Path(inspect.getsourcefile(initialize_mcp_server)).read_text()
    assert "route_map_fn=openapi_route_map" in text
    assert "timeout=300" in text
    assert "from fastmcp.server.providers.openapi.routing import MCPType" in text
    assert "stateless_http=True" in text
    assert "host_origin_protection=True" in text
    assert "FASTMCP_EXPERIMENTAL_ENABLE_NEW_OPENAPI_PARSER" not in text


def _assert_not_read_only(tool) -> None:
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.idempotent_hint is False
    assert "Side effects: none" not in (tool.description or "")
    assert changes_tenant_state(tool)


@pytest.mark.asyncio
async def test_issue_26_tools_are_hidden_until_their_flags_are_set(feature_flags, bundled_spec):
    feature_flags(write=False, isolate=False, secret=False)
    hidden = await _tool_map(_annotated_catalog(bundled_spec))
    assert "post_endpoints_file_retrieval" not in hidden
    for name in SECRET_CATALOG_TOOLS:
        assert name not in hidden
    assert "get_assets" in hidden
    assets = hidden["get_assets"]
    assert assets.annotations is not None
    assert assets.annotations.read_only_hint is True
    assert "Side effects: none" in (assets.description or "")

    feature_flags(write=True, isolate=False, secret=False)
    writes = await _tool_map(_annotated_catalog(bundled_spec))
    _assert_not_read_only(writes["post_endpoints_file_retrieval"])
    for name in SECRET_CATALOG_TOOLS:
        assert name not in writes

    feature_flags(write=True, isolate=False, secret=True)
    exposed = await _tool_map(_annotated_catalog(bundled_spec))
    for name in ISSUE_26_TOOLS:
        _assert_not_read_only(exposed[name])
    _assert_not_read_only(exposed["get_brokers_images"])
    for tool in exposed.values():
        description = tool.description or ""
        read_only = tool.annotations is not None and tool.annotations.read_only_hint is True
        if "Side effects: none" in description:
            assert read_only
        if tool.annotations is not None and tool.annotations.read_only_hint is False:
            assert "Side effects: none" not in description


@pytest.mark.asyncio
async def test_consumption_and_file_retrieval_details_are_read_only(feature_flags, bundled_spec):
    feature_flags(write=False, isolate=False, secret=False)
    tools = await _tool_map(_annotated_catalog(bundled_spec))
    for name in READ_LOOKUP_TOOLS:
        tool = tools[name]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert (tool.description or "").startswith("Side effects: none.")
        assert changes_tenant_state(tool) is False
    assert "post_endpoints_file_retrieval" not in tools
    for name in SECRET_CATALOG_TOOLS:
        assert name not in tools

    for _path, methods in bundled_spec["paths"].items():
        for operation in methods.values():
            if not isinstance(operation, dict):
                continue
            if operation.get("operationId") in READ_LOOKUP_TOOLS:
                assert operation[SIDE_EFFECT_EXTENSION] == "read"
                assert operation["description"].startswith("Side effects: none.")


@pytest.mark.asyncio
async def test_file_retrieval_middleware_asks_before_running(feature_flags, bundled_spec, monkeypatch):
    feature_flags(write=True, isolate=False, secret=False)
    monkeypatch.setenv("MCP_ELICITATION_ENABLED", "true")
    reload_config()
    try:
        server = _annotated_catalog(bundled_spec)

        class _Session:
            def check_client_capability(self, capability):
                return True

        class _Ctx:
            def __init__(self):
                self.fastmcp = server
                self.session = _Session()
                self.request_context = object()
                self.elicited = False

            async def elicit(self, message, response_type):
                self.elicited = True
                assert response_type is WriteConfirmation
                assert "post_endpoints_file_retrieval" in message
                return AcceptedElicitation(data=WriteConfirmation(confirm=False))

        ctx = _Ctx()
        context = MiddlewareContext(
            message=CallToolRequestParams(name="post_endpoints_file_retrieval", arguments={}),
            fastmcp_context=ctx,
            method="tools/call",
            type="request",
        )

        async def call_next(seen):
            raise AssertionError("the tool ran")

        refused = await WriteConfirmationMiddleware().on_call_tool(context, call_next)
        assert ctx.elicited is True
        assert refused.is_error is True
        assert "not confirmed" in refused.content[0].text
    finally:
        monkeypatch.delenv("MCP_ELICITATION_ENABLED", raising=False)
        reload_config()


def test_fastmcp_debug_logs_stay_quiet():
    configure_library_logging()
    assert logging.getLogger("fastmcp").level == logging.WARNING


def test_fetcher_does_not_log_the_api_key_id():
    text = Path(inspect.getsourcefile(get_fetcher)).read_text()
    function_text = text.split("def get_fetcher", 1)[1]
    assert 'logger.info("Creating a new Cortex API fetcher")' in function_text
    assert "api_key_id" not in function_text


@pytest.mark.asyncio
async def test_openapi_client_restores_tenant_credentials():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {key.lower(): value for key, value in request.headers.items()}
        return httpx.Response(200, json={"ok": True})

    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(handler),
    )
    request = client.build_request(
        "POST",
        "/public_api/v1/case/search",
        headers={
            "Authorization": "Bearer inbound-mcp-token",
            "x-xdr-auth-id": "spoofed",
            "Cookie": "session=abc",
            "X-Custom": "from-client",
            "Content-Type": "application/json",
        },
        json={"request_data": {}},
    )
    response = await client.send(request)
    await client.aclose()

    assert response.status_code == 200
    headers = captured["headers"]
    assert headers["authorization"] == "tenant-secret"
    assert headers["x-xdr-auth-id"] == "42"
    assert "cookie" not in headers
    assert "x-custom" not in headers
    assert headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_non_json_success_raises_response_error():
    client = PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not-json")),
    )
    with pytest.raises(PAPIResponseError):
        await client.request("POST", "/public_api/v1/scripts/insert")
    await client.aclose()


def test_decode_xql_stream_ungzips_json_and_wraps_text():
    raw = gzip.compress(b'{"reply":{"status":"SUCCESS"}}')
    assert decode_xql_stream(raw) == {"reply": {"status": "SUCCESS"}}
    assert decode_xql_stream(b'{"results":[1]}') == {"results": [1]}
    assert decode_xql_stream(b"plain text") == {"results": "plain text"}
    assert decode_xql_stream(b"[1,2]") == {"results": [1, 2]}


@pytest.mark.asyncio
async def test_run_xql_query_polls_the_documented_results_path(monkeypatch):
    calls = []

    class FakeFetcher:
        async def send_request(self, path, **kwargs):
            calls.append((path, kwargs.get("data")))
            if path.startswith("xql/start"):
                return {"reply": "exec-1"}
            return {"reply": {"status": "SUCCESS", "results": []}}

    async def fake_get_fetcher(ctx):
        return FakeFetcher()

    monkeypatch.setattr(xql_query_module, "get_fetcher", fake_get_fetcher)
    result = await run_xql_query(None, "dataset=xdr_data | limit 1", timeframe={"relativeTime": 86400000}, limit=10)

    payload = json.loads(result)
    assert payload["reply"]["status"] == "SUCCESS"
    assert calls[0][0] == "xql/start_xql_query/"
    assert calls[1][0] == "xql/get_query_results"
    assert calls[1][1]["request_data"]["query_id"] == "exec-1"
    assert calls[1][1]["request_data"]["pending_flag"] is True
    assert calls[1][1]["request_data"]["format"] == "json"
    assert "get_xql_query_results" not in calls[1][0]
    timeframe = inspect.signature(run_xql_query).parameters["timeframe"].annotation
    assert "milliseconds" in timeframe.__metadata__[0].description
