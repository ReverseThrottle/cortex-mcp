import pytest

from config.config import Settings, reload_config
from main import initialize_mcp_server
from pkg import tool_classification
from pkg.startup_checks import check_tool_controls, known_tool_classes
from pkg.tool_classification import catalog_tool_enabled

_ENV = (
    "MCP_WRITE_TOOLS_ENABLED",
    "MCP_SECRET_TOOLS_ENABLED",
    "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED",
    "MCP_TOOLS_ALLOW",
    "MCP_TOOLS_DENY",
)


@pytest.fixture
def tool_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)

    def apply(**values: str):
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return reload_config()

    yield apply
    monkeypatch.undo()
    reload_config()


async def _names() -> set[str]:
    server = await initialize_mcp_server("dummy-key", "1", "example.invalid")
    return {tool.name for tool in await server.list_tools()}


@pytest.mark.asyncio
async def test_allow_registers_named_write_tools_without_the_write_flag(tool_env):
    tool_env(MCP_WRITE_TOOLS_ENABLED="false")
    baseline = await _names()
    tool_env(MCP_TOOLS_ALLOW="update_case,post_lists_save")
    names = await _names()
    assert {"update_case", "post_lists_save"} <= names
    assert "insert_script" not in names
    assert "post_lists_delete" not in names
    assert len(names) == 255 + 2
    # Nothing else moved: the registered set differs from the baseline by exactly the two named tools.
    assert names ^ baseline == {"update_case", "post_lists_save"}
    assert names - baseline == {"update_case", "post_lists_save"}


@pytest.mark.asyncio
async def test_deny_removes_tools_even_when_writes_are_enabled(tool_env):
    tool_env(MCP_WRITE_TOOLS_ENABLED="true")
    baseline = await _names()
    tool_env(MCP_TOOLS_DENY="update_case,post_lists_delete,get_cases")
    names = await _names()
    assert not {"update_case", "post_lists_delete", "get_cases"} & names
    assert len(names) == 523 - 3
    assert names ^ baseline == {"update_case", "post_lists_delete", "get_cases"}
    assert baseline - names == {"update_case", "post_lists_delete", "get_cases"}


@pytest.mark.asyncio
async def test_deny_wins_over_allow(tool_env):
    tool_env(MCP_WRITE_TOOLS_ENABLED="false")
    baseline = await _names()
    tool_env(MCP_TOOLS_ALLOW="update_case", MCP_TOOLS_DENY="update_case")
    names = await _names()
    assert "update_case" not in names
    assert names ^ baseline == set()


# Every handwritten write/action tool gated by write_tool_enabled() outside
# update_case (which the tests above already cover). One call site per module:
# broker_vm.py (looped over _BROKER_TOOLS), content_uploads.py, xsoar_actions.py,
# xql_query.py. See issue #121.
_OTHER_HANDWRITTEN_WRITE_TOOLS = (
    "post_auth_reset_initial_password",
    "post_auth_token",
    "post_logs",
    "post_network_interface",
    "post_network_internal_subnet",
    "post_network_proxy",
    "post_network_ntp",
    "post_network_ssl_certificate",
    "post_network_trusted_ca",
    "post_register",
    "insert_script",
    "insert_playbook",
    "post_playbook_save_yaml",
    "post_entry_upload_by_incident_id",
    "post_incident_upload_by_incident_id",
    "post_inv_playbook_task_complete",
    "put_settings_credentials",
    "run_xql_query",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _OTHER_HANDWRITTEN_WRITE_TOOLS)
async def test_allow_registers_each_handwritten_write_tool_without_the_write_flag(tool_env, name):
    tool_env(MCP_WRITE_TOOLS_ENABLED="false")
    off = await _names()
    assert name not in off
    tool_env(MCP_TOOLS_ALLOW=name)
    names = await _names()
    assert name in names
    assert names - off == {name}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _OTHER_HANDWRITTEN_WRITE_TOOLS)
async def test_deny_removes_each_handwritten_write_tool_even_when_writes_are_enabled(tool_env, name):
    tool_env(MCP_WRITE_TOOLS_ENABLED="true")
    baseline = await _names()
    assert name in baseline
    tool_env(MCP_TOOLS_DENY=name)
    names = await _names()
    assert name not in names
    assert baseline - names == {name}


@pytest.mark.asyncio
async def test_isolate_tools_keep_their_own_flag(tool_env):
    tool_env(MCP_WRITE_TOOLS_ENABLED="true", MCP_ISOLATE_ENDPOINT_TOOL_ENABLED="true")
    baseline = await _names()
    assert {"isolate_endpoint", "unisolate_endpoint"} <= baseline
    tool_env(MCP_TOOLS_DENY="isolate_endpoint")
    names = await _names()
    assert "isolate_endpoint" not in names
    assert "unisolate_endpoint" in names
    assert names ^ baseline == {"isolate_endpoint"}


@pytest.mark.asyncio
async def test_isolate_tools_register_on_their_own_flag_without_the_write_flag(tool_env):
    isolate = {"isolate_endpoint", "unisolate_endpoint"}
    tool_env(MCP_WRITE_TOOLS_ENABLED="false", MCP_ISOLATE_ENDPOINT_TOOL_ENABLED="false")
    off = await _names()
    assert not isolate & off
    tool_env(MCP_ISOLATE_ENDPOINT_TOOL_ENABLED="true")
    names = await _names()
    assert isolate <= names
    assert names - off == isolate
    assert len(names) == 255 + 2
    tool_env(MCP_TOOLS_DENY="isolate_endpoint,unisolate_endpoint")
    denied = await _names()
    assert not isolate & denied
    assert denied == off


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["false", "true"])
async def test_allow_does_not_register_isolate_tools_when_their_flag_is_off(tool_env, write):
    tool_env(MCP_WRITE_TOOLS_ENABLED=write, MCP_ISOLATE_ENDPOINT_TOOL_ENABLED="false")
    baseline = await _names()
    tool_env(MCP_TOOLS_ALLOW="isolate_endpoint,unisolate_endpoint")
    names = await _names()
    assert not {"isolate_endpoint", "unisolate_endpoint"} & names
    assert names ^ baseline == set()


def _settings(tool_env, **values: str) -> Settings:
    tool_env(**values)
    return Settings(_env_file=None)


def test_known_tool_classes_cover_handwritten_and_catalog():
    known = known_tool_classes()
    assert known["update_case"] == "write"
    assert known["post_lists_save"] == "write"
    assert known["get_cases"] == "read"
    assert "isolate_endpoint" in known


def test_unknown_names_stop_startup(tool_env):
    with pytest.raises(ValueError, match="not_a_tool"):
        check_tool_controls(_settings(tool_env, MCP_TOOLS_DENY="not_a_tool"))
    with pytest.raises(ValueError, match="not_a_tool"):
        check_tool_controls(_settings(tool_env, MCP_TOOLS_ALLOW="not_a_tool"))


def test_allow_rejects_read_secret_and_isolate_tools(tool_env):
    secret = next(name for name, kind in known_tool_classes().items() if kind == "secret")
    for name in (secret, "get_cases", "isolate_endpoint", "unisolate_endpoint"):
        with pytest.raises(ValueError, match=name):
            check_tool_controls(_settings(tool_env, MCP_TOOLS_ALLOW=name))


def test_valid_lists_pass(tool_env):
    check_tool_controls(_settings(tool_env, MCP_TOOLS_ALLOW="update_case", MCP_TOOLS_DENY="isolate_endpoint,get_cases"))


def _secret_tool() -> str:
    return next(name for name, kind in sorted(known_tool_classes().items()) if kind == "secret")


@pytest.mark.parametrize(
    "flags",
    [
        {"MCP_WRITE_TOOLS_ENABLED": "false", "MCP_SECRET_TOOLS_ENABLED": "false"},
        {"MCP_WRITE_TOOLS_ENABLED": "true", "MCP_SECRET_TOOLS_ENABLED": "false"},
        {"MCP_WRITE_TOOLS_ENABLED": "false", "MCP_SECRET_TOOLS_ENABLED": "true"},
    ],
)
def test_allow_never_lifts_the_secret_flags(tool_env, flags):
    name = _secret_tool()
    tool_env(MCP_TOOLS_ALLOW=name, **flags)
    assert catalog_tool_enabled("secret", name) is False


def test_secret_tools_need_both_flags_and_deny_still_wins(tool_env):
    name = _secret_tool()
    tool_env(MCP_WRITE_TOOLS_ENABLED="true", MCP_SECRET_TOOLS_ENABLED="true")
    assert catalog_tool_enabled("secret", name) is True
    tool_env(MCP_TOOLS_DENY=name)
    assert catalog_tool_enabled("secret", name) is False


def test_allow_does_not_lift_an_unclassified_catalog_tool(tool_env):
    tool_env(MCP_WRITE_TOOLS_ENABLED="false", MCP_TOOLS_ALLOW="brand_new_op")
    assert catalog_tool_enabled(None, "brand_new_op") is False
    tool_env(MCP_WRITE_TOOLS_ENABLED="true")
    assert catalog_tool_enabled(None, "brand_new_op") is True


def _with_unclassified_catalog_operation(monkeypatch, operation_id: str = "brand_new_op") -> None:
    real = list(tool_classification._iter_catalog_operations())
    stubbed = [*real, (operation_id, {"summary": "no side-effect extension"})]
    monkeypatch.setattr(tool_classification, "_iter_catalog_operations", lambda: iter(stubbed))


def test_catalog_operation_ids_include_unclassified_operations(monkeypatch):
    real = tool_classification.catalog_operation_ids()
    assert set(tool_classification.catalog_classifications()) <= real
    _with_unclassified_catalog_operation(monkeypatch)
    assert tool_classification.catalog_operation_ids() == real | {"brand_new_op"}


def test_known_tool_classes_lists_an_unclassified_catalog_operation_as_none(monkeypatch):
    _with_unclassified_catalog_operation(monkeypatch)
    known = known_tool_classes()
    assert "brand_new_op" in known
    assert known["brand_new_op"] is None
    assert known["update_case"] == "write"
    assert known["get_cases"] == "read"


def test_deny_accepts_an_unclassified_catalog_operation(tool_env, monkeypatch):
    _with_unclassified_catalog_operation(monkeypatch)
    check_tool_controls(_settings(tool_env, MCP_TOOLS_DENY="brand_new_op"))


def test_allow_rejects_an_unclassified_catalog_operation(tool_env, monkeypatch):
    _with_unclassified_catalog_operation(monkeypatch)
    with pytest.raises(ValueError, match="can only enable write or action tools") as raised:
        check_tool_controls(_settings(tool_env, MCP_TOOLS_ALLOW="brand_new_op"))
    assert "brand_new_op" in str(raised.value)
    assert "unknown" not in str(raised.value)


def test_a_name_that_is_not_in_the_catalog_is_still_unknown(tool_env, monkeypatch):
    _with_unclassified_catalog_operation(monkeypatch)
    with pytest.raises(ValueError, match="unknown tools: other_op"):
        check_tool_controls(_settings(tool_env, MCP_TOOLS_DENY="brand_new_op,other_op"))
