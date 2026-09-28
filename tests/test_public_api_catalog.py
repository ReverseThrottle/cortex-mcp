import re

import httpx
import pytest
from fastmcp import FastMCP

from pkg.client import PAPIClient
from pkg.util import bundle_openapi_from_folders
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
    "POST /public_api/v1/xql/get_query_results_stream",
    "POST /public_api/v1/xql/get_quota",
    "POST /public_api/v1/endpoints/get_endpoints",
    "GET /public_api/appsec/v1/package_explorer/packages/{name}/versions/{version}",
)

PYTHON_TOOLS = {
    "get_cases",
    "get_issues",
    "update_case",
    "isolate_endpoint",
    "unisolate_endpoint",
    "run_xql_query",
    "insert_script",
    "insert_playbook",
}


def _norm(method: str, path: str) -> tuple[str, str]:
    cleaned = path.rstrip("/") or "/"
    cleaned = re.sub(r"\{[^}]+\}", "{}", cleaned)
    return method.upper(), cleaned


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


def test_catalog_covers_documented_operations_without_credential_parameters(bundled_spec, openapi_server):
    tools = openapi_server._tool_manager._tools
    assert len(tools) == 513

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


def test_python_tools_register_without_colliding_with_the_catalog(openapi_server):
    mcp = FastMCP("helpers")
    discover_and_register_modules(mcp)
    helper_names = set(mcp._tool_manager._tools)
    assert PYTHON_TOOLS <= helper_names
    assert helper_names.isdisjoint(openapi_server._tool_manager._tools)

    for name in ("insert_script", "insert_playbook", "update_case", "isolate_endpoint", "run_xql_query"):
        assert "Side effects:" in (mcp._tool_manager._tools[name].description or "")


def test_openapi_client_restores_tenant_credentials():
    client = PAPIClient(
        "https://api.example.invalid",
        {"Authorization": "tenant-secret", "x-xdr-auth-id": "42"},
    )
    request = httpx.Request(
        "POST",
        "https://api.example.invalid/public_api/v1/case/search",
        headers={"Authorization": "Bearer inbound-mcp-token", "x-xdr-auth-id": "spoofed"},
    )
    for key, value in client._papi_headers.items():
        request.headers[key] = value
    assert request.headers["authorization"] == "tenant-secret"
    assert request.headers["x-xdr-auth-id"] == "42"
