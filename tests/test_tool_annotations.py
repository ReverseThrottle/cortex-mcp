import pytest
from fastmcp import FastMCP

from config.config import reload_config
from pkg.tool_classification import (
    ADDITIVE_EXTENSION,
    HANDWRITTEN_ADDITIVE,
    HANDWRITTEN_SIDE_EFFECTS,
    additive_from_route,
    catalog_classifications,
)
from pkg.tool_presentation import annotations_for_side_effect
from pkg.util import bundle_openapi_from_folders
from usecase.module_util import discover_and_register_modules


@pytest.mark.parametrize(
    ("kind", "additive", "read_only", "destructive", "idempotent"),
    [
        ("read", False, True, False, True),
        ("write", False, False, True, False),
        ("write", True, False, False, False),
        ("action", False, False, True, False),
        ("action", True, False, False, False),
        ("secret", False, False, False, False),
        (None, False, False, True, False),
    ],
)
def test_annotation_matrix(kind, additive, read_only, destructive, idempotent):
    annotations = annotations_for_side_effect(kind, additive=additive)
    assert annotations.read_only_hint is read_only
    assert annotations.destructive_hint is destructive
    assert annotations.idempotent_hint is idempotent
    assert annotations.open_world_hint is True


def test_additive_never_applies_to_unclassified_tools():
    assert annotations_for_side_effect(None, additive=True).destructive_hint is True


class _Route:
    def __init__(self, extensions=None, operation_id=None):
        self.extensions = extensions or {}
        self.operation_id = operation_id


def test_route_extension_marks_only_write_and_action_routes_additive():
    side = "x-cortex-mcp-side-effect"
    assert additive_from_route(_Route({side: "write", ADDITIVE_EXTENSION: False})) is True
    assert additive_from_route(_Route({side: "action", ADDITIVE_EXTENSION: False})) is True
    assert additive_from_route(_Route({side: "write"})) is False
    assert additive_from_route(_Route({side: "write", ADDITIVE_EXTENSION: True})) is False
    assert additive_from_route(_Route({side: "read", ADDITIVE_EXTENSION: False})) is False
    assert additive_from_route(_Route({side: "secret", ADDITIVE_EXTENSION: False})) is False


def test_catalog_additive_markers_are_valid():
    spec = bundle_openapi_from_folders()
    bad = []
    for methods in spec.get("paths", {}).values():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                continue
            if ADDITIVE_EXTENSION not in operation:
                continue
            problems = []
            if operation[ADDITIVE_EXTENSION] is not False:
                problems.append("value must be false")
            if operation.get("x-cortex-mcp-side-effect") not in {"write", "action"}:
                problems.append("only write/action operations can be additive")
            if method.lower() == "delete":
                problems.append("a DELETE operation cannot be additive")
            if problems:
                bad.append(f"{operation.get('operationId')}: {', '.join(problems)}")
    assert bad == []


def test_handwritten_additive_names_are_handwritten_write_or_action_tools():
    for name in HANDWRITTEN_ADDITIVE:
        assert HANDWRITTEN_SIDE_EFFECTS.get(name) in {"write", "action"}, name


def test_catalog_classifications_returns_a_copy():
    first = catalog_classifications()
    assert first and first is not catalog_classifications()
    expected = dict(first)
    victim = next(iter(first))
    first[victim] = "read" if first[victim] != "read" else "write"
    first["not_a_catalog_operation"] = "write"
    del first[next(name for name in first if name not in {victim, "not_a_catalog_operation"})]
    assert catalog_classifications() == expected


@pytest.fixture
def write_tools(monkeypatch):
    monkeypatch.setenv("MCP_WRITE_TOOLS_ENABLED", "true")
    monkeypatch.setenv("MCP_ISOLATE_ENDPOINT_TOOL_ENABLED", "true")
    reload_config()
    yield
    monkeypatch.undo()
    reload_config()


@pytest.mark.asyncio
async def test_handwritten_tools_publish_destructive_by_default(write_tools):
    server = FastMCP("handwritten")
    discover_and_register_modules(server)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["update_case"].annotations.destructive_hint is True
    assert tools["isolate_endpoint"].annotations.destructive_hint is True
    assert tools["unisolate_endpoint"].annotations.destructive_hint is True
    assert tools["get_cases"].annotations.read_only_hint is True
    assert tools["get_cases"].annotations.destructive_hint is False


APPROVED_ADDITIVE = frozenset(
    {
        "post_brokers_by_device_id_logs_generate",
        "post_case_timeline_by_case_id_add_record",
        "post_entries_insert",
        "post_cloud_onboarding_create_instance_template",
        "post_cloud_onboarding_create_outpost_template",
        "post_compliance_add_assessment_profile",
        "post_compliance_add_control",
        "post_compliance_add_rules_to_control",
        "post_compliance_add_standard",
        "post_api_keys_generate",
        "post_asset_groups_create",
        "post_authentication_settings_create",
        "post_xql_add_dataset",
        "post_distributions_create",
        "post_tags_agents_create",
        "post_legacy_exceptions_add",
        "post_profiles_prevention_add",
        "post_integrations_syslog_create",
        "post_dataset_define_dataset",
        "post_mth_child_add_comment",
        "post_uvm_public_v1_create_policy",
        "post_alerts_insert_parsed_alerts",
        "post_alerts_insert_cef_alerts",
        "post_asm_management_upload_asm_data",
        "post_assets_create_asset_tag_rules",
        "post_indicator_create",
        "post_inv_playbook_task_add_by_investigationid",
    }
)


@pytest.mark.asyncio
async def test_catalog_annotations_follow_the_additive_exceptions(write_tools):
    from main import initialize_mcp_server

    server = await initialize_mcp_server("dummy-key", "1", "example.invalid")
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["post_lists_delete"].annotations.destructive_hint is True
    assert tools["post_endpoints_delete"].annotations.destructive_hint is True
    assert tools["post_asset_groups_create"].annotations.read_only_hint is False
    assert tools["post_asset_groups_create"].annotations.destructive_hint is False
    assert tools["get_assets"].annotations.destructive_hint is False
    # Owner-vetoed operations stay destructive (upserts can overwrite existing data).
    for vetoed in ("post_bioc_insert", "post_indicators_insert", "post_xql_lookups_add_data"):
        assert tools[vetoed].annotations.destructive_hint is True, vetoed
    assert tools["post_api_keys_generate"].annotations.destructive_hint is False


@pytest.mark.asyncio
async def test_exactly_the_approved_write_class_operations_are_additive(write_tools):
    from main import initialize_mcp_server

    server = await initialize_mcp_server("dummy-key", "1", "example.invalid")
    tools = {tool.name: tool for tool in await server.list_tools()}
    write_class = {name for name, kind in catalog_classifications().items() if kind in {"write", "action"}}
    registered = write_class & tools.keys()
    assert registered, "no write-class catalog tools registered"
    additive = {name for name in registered if tools[name].annotations.destructive_hint is False}
    assert additive == APPROVED_ADDITIVE
