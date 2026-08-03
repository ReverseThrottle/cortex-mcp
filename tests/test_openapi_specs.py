"""
Regression net for the OpenAPI-spec-driven tools (builtin_components/openapi/, custom_components/openapi/).

Every read-only tool in this repo is a YAML file, not Python — FastMCP.from_openapi()
turns the bundled spec into tools automatically. A typo in one YAML file (bad $ref,
duplicate operationId, malformed path) fails silently at startup otherwise. This test
catches that before any tenant is involved.
"""

import httpx
import pytest
from fastmcp import FastMCP

from pkg.util import bundle_openapi_from_folders

# The 6 read tools that exist before this build-out started. As new domains are added,
# extend this set rather than replacing it, so a regression in an existing tool is caught too.
BASELINE_OPERATION_IDS = {
    "get_assessment_profile_results",
    "get_assets",
    "get_asset_by_id",
    "get_filtered_endpoints",
    "get_tenant_info",
    "get_vulnerabilities",
    # Added during the API gap build-out — verified against a live tenant, see commit history.
    "get_cases_schema",
    "get_case_extra_data",
}


def test_bundled_spec_parses():
    spec = bundle_openapi_from_folders()
    assert spec is not None
    assert "paths" in spec
    assert len(spec["paths"]) > 0


def test_bundled_spec_has_no_duplicate_operation_ids():
    spec = bundle_openapi_from_folders()
    seen: dict[str, str] = {}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            op_id = operation["operationId"]
            location = f"{method.upper()} {path}"
            assert op_id not in seen, f"Duplicate operationId '{op_id}' at {location} (first seen at {seen[op_id]})"
            seen[op_id] = location


def test_baseline_operations_present():
    spec = bundle_openapi_from_folders()
    found_ids = {
        operation["operationId"]
        for methods in spec["paths"].values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    missing = BASELINE_OPERATION_IDS - found_ids
    assert not missing, f"Expected baseline operationIds missing from bundled spec: {missing}"


@pytest.mark.asyncio
async def test_spec_loads_into_fastmcp_without_error():
    """FastMCP.from_openapi must be able to parse the full bundled spec and register a tool
    per operation. This is the actual mechanism main.py uses at startup."""
    spec = bundle_openapi_from_folders()
    dummy_client = httpx.AsyncClient(base_url="https://api-test.xdr.us.paloaltonetworks.com")
    openapi_mcp = FastMCP.from_openapi(spec, dummy_client)
    tools = await openapi_mcp.get_tools()
    assert BASELINE_OPERATION_IDS.issubset(tools.keys())
