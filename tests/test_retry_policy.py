"""Route classification used by the ReadTimeout retry policy (issue #46)."""

import pytest

from pkg.retry_policy import (
    _HANDWRITTEN_ROUTES,
    may_have_side_effects,
    read_timeout_is_retryable,
    route_side_effect,
)
from pkg.tool_classification import HANDWRITTEN_SIDE_EFFECTS
from usecase.builtin_components.broker_vm import _BROKER_TOOLS

_BASE = "https://api-example.example.invalid"


@pytest.mark.parametrize(
    ("method", "path", "kind"),
    [
        ("POST", "/public_api/v1/scripts/run_script", "write"),
        ("POST", "/public_api/v1/api_keys/generate", "write"),
        ("POST", "/public_api/v1/playbooks/get", "read"),
        ("POST", "/public_api/v1/endpoints/get_endpoints", "read"),
        ("POST", "/public_api/v1/brokers/dev-1/applets/wec/wef_cert/", "secret"),
        ("PUT", "/public_api/appsec/v1/policies/p-123", "write"),
        ("DELETE", "/public_api/appsec/v1/rules/r-9", "write"),
        ("GET", "/public_api/appsec/v1/policies/p-123", "read"),
        ("GET", "/public_api/v1/assets/asset-42/", "read"),
    ],
)
def test_catalog_routes_use_the_spec_classification(method, path, kind):
    assert route_side_effect(method, f"{_BASE}{path}") == kind


@pytest.mark.parametrize(
    ("method", "path", "tool"),
    [
        ("POST", "/public_api/v1/endpoints/isolate/", "isolate_endpoint"),
        ("POST", "/public_api/v1/endpoints/unisolate/", "unisolate_endpoint"),
        ("POST", "/public_api/v1/case/update/4", "update_case"),
        ("POST", "/public_api/v1/case/search/", "get_cases"),
        ("POST", "/public_api/v1/issue/search/", "get_issues"),
        ("POST", "/public_api/v1/xql/start_xql_query/", "run_xql_query"),
        ("POST", "/public_api/v1/scripts/insert", "insert_script"),
        ("POST", "/public_api/v1/playbooks/insert", "insert_playbook"),
        ("POST", "/xsoar/public/v1/playbook/save/yaml", "post_playbook_save_yaml"),
        ("POST", "/xsoar/public/v1/entry/upload/42", "post_entry_upload_by_incident_id"),
        ("POST", "/xsoar/public/v1/incident/upload/42", "post_incident_upload_by_incident_id"),
        ("POST", "/xsoar/public/v1/inv-playbook/task/complete", "post_inv_playbook_task_complete"),
        ("PUT", "/xsoar/public/v1/settings/credentials", "put_settings_credentials"),
        ("GET", "/xsoar/public/v1/performance/incident/export/42", "get_performance_incident_export_by_incident_id"),
    ],
)
def test_handwritten_routes_use_the_registry_class(method, path, tool):
    assert route_side_effect(method, f"{_BASE}{path}") == HANDWRITTEN_SIDE_EFFECTS[tool]


def test_every_handwritten_tool_has_a_route_or_uses_the_broker_client():
    # Fail-closed completeness: a new handwritten tool must land in the route
    # table (PAPIClient) or in _BROKER_TOOLS (BrokerClient, which never retries).
    broker_names = {tool.__name__ for tool in _BROKER_TOOLS}
    routed_names = set(_HANDWRITTEN_ROUTES.values())
    assert broker_names.isdisjoint(routed_names)
    assert broker_names | routed_names == set(HANDWRITTEN_SIDE_EFFECTS)


def test_content_download_helper_is_a_read():
    assert route_side_effect("POST", f"{_BASE}/public_api/v1/mcp/download/") == "read"
    assert read_timeout_is_retryable("POST", f"{_BASE}/public_api/v1/mcp/download/") is True


@pytest.mark.parametrize(
    "url",
    [
        f"{_BASE}/public_api/v1/endpoints/isolate/",
        f"{_BASE}/public_api/v1/endpoints/isolate",
        f"{_BASE}/public_api/v1/endpoints/isolate?x=1",
        f"{_BASE}/PUBLIC_API/V1/ENDPOINTS/ISOLATE/",
        "/public_api/v1/endpoints/isolate/",
    ],
)
def test_lookup_ignores_host_query_case_and_trailing_slash(url):
    assert route_side_effect("POST", url) == "action"


def test_unknown_route_is_unclassified_and_fail_safe():
    url = f"{_BASE}/public_api/v1/brand/new_endpoint"
    assert route_side_effect("POST", url) is None
    # An unclassified route is not read-only: a non-read method may mutate.
    assert may_have_side_effects("POST", url) is True
    assert may_have_side_effects("PUT", url) is True
    assert may_have_side_effects("PATCH", url) is True
    assert may_have_side_effects("DELETE", url) is True
    assert read_timeout_is_retryable("POST", url) is False
    # Read methods stay retryable without a classification.
    assert may_have_side_effects("GET", url) is False
    assert read_timeout_is_retryable("GET", url) is True
    assert read_timeout_is_retryable("HEAD", url) is True
    assert read_timeout_is_retryable("OPTIONS", url) is True


@pytest.mark.parametrize(
    ("method", "path", "retryable"),
    [
        ("POST", "/public_api/v1/endpoints/isolate/", False),  # action
        ("POST", "/public_api/v1/scripts/run_script", False),  # write
        ("POST", "/public_api/v1/brokers/dev-1/applets/wec/wef_cert/", False),  # secret
        ("POST", "/public_api/v1/playbooks/get", True),  # read
        ("POST", "/public_api/v1/xql/get_query_results", True),  # read (XQL polling)
        ("POST", "/public_api/v1/incidents/search", False),  # slow search read
        ("POST", "/public_api/v1/case/search/", False),  # slow search read
        ("POST", "/public_api/v1/mth/child/get_all_reports", False),  # slow report read
    ],
)
def test_read_timeout_retryability(method, path, retryable):
    assert read_timeout_is_retryable(method, f"{_BASE}{path}") is retryable


def test_path_parameter_templates_need_a_concrete_segment():
    assert route_side_effect("PUT", f"{_BASE}/public_api/appsec/v1/policies/") is None
    assert route_side_effect("PUT", f"{_BASE}/public_api/appsec/v1/policies/p-1/extra") is None
    assert route_side_effect("POST", f"{_BASE}/xsoar/public/v1/entry/upload/") is None
