import base64
import json
import os

import httpx
import pytest
from fastmcp import FastMCP

from config.config import reload_config
from pkg.broker_client import BrokerClient, broker_base_url, reset_broker_client
from pkg.write_confirmation import changes_tenant_state
from usecase.builtin_components import broker_vm
from usecase.builtin_components.broker_vm import (
    _BROKER_TOOLS,
    post_auth_reset_initial_password,
    post_auth_token,
    post_logs,
    post_network_interface,
    post_network_ntp,
)
from usecase.module_util import discover_and_register_modules

_FACTORY = "factory-secret"
_TOKEN = "broker-token"
_MCP_BEARER = "Bearer inbound-mcp-token"


def _headers(request: httpx.Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.headers.items()}


def _client(handler) -> BrokerClient:
    return BrokerClient("https://broker.example", _FACTORY, transport=httpx.MockTransport(handler))


@pytest.fixture
def server_settings(monkeypatch):
    names = (
        "MCP_WRITE_TOOLS_ENABLED",
        "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED",
        "CORTEX_MCP_BROKER_URL",
        "CORTEX_MCP_BROKER_FACTORY_PASSWORD",
    )
    previous = {name: os.environ.get(name) for name in names}

    def apply(**values):
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        reload_config()
        reset_broker_client()

    yield apply

    for name, value in previous.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    reload_config()
    reset_broker_client()


def test_broker_url_requires_https():
    assert broker_base_url("broker.example") == "https://broker.example"
    with pytest.raises(Exception, match="https"):
        broker_base_url("http://broker.example")


def test_broker_tools_change_state_and_register_only_with_writes(server_settings):
    for tool in _BROKER_TOOLS:
        assert changes_tenant_state(tool.__doc__)
        assert "Side effects: none" not in (tool.__doc__ or "")

    server_settings(MCP_WRITE_TOOLS_ENABLED="false")
    hidden = FastMCP("hidden")
    discover_and_register_modules(hidden)
    hidden_names = set(hidden._tool_manager._tools)
    assert {tool.__name__ for tool in _BROKER_TOOLS}.isdisjoint(hidden_names)

    server_settings(MCP_WRITE_TOOLS_ENABLED="true")
    shown = FastMCP("shown")
    discover_and_register_modules(shown)
    assert {tool.__name__ for tool in _BROKER_TOOLS} <= set(shown._tool_manager._tools)


@pytest.mark.asyncio
async def test_broker_login_uses_factory_password_and_not_the_mcp_bearer(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = _headers(request)
        assert _MCP_BEARER not in headers.values()
        assert "x-xdr-auth-id" not in headers
        if request.url.path == "/public_api/v1/auth/token":
            assert "authorization" not in headers
            assert json.loads(request.content) == {"password": _FACTORY}
            return httpx.Response(200, json={"reply": {"api_key": _TOKEN}})
        assert headers["authorization"] == f"Bearer {_TOKEN}"
        assert json.loads(request.content) == {"ntp": ["time.example"]}
        return httpx.Response(200, json={"reply": None})

    client = _client(handler)
    monkeypatch.setattr(broker_vm, "get_broker_client", lambda: client)
    first = json.loads(await post_network_ntp(None, ["time.example"]))
    second = json.loads(await post_network_ntp(None, ["time.example"]))
    assert first["success"] == "true"
    assert second["success"] == "true"
    assert [request.url.path for request in seen] == [
        "/public_api/v1/auth/token",
        "/public_api/v1/network/ntp",
        "/public_api/v1/network/ntp",
    ]


@pytest.mark.asyncio
async def test_token_tool_hides_the_broker_bearer_and_reset_keeps_the_new_password(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/public_api/v1/auth/token":
            return httpx.Response(200, json={"reply": {"api_key": _TOKEN}})
        body = json.loads(request.content)
        assert body["current_password"] == _FACTORY
        assert body["new_password"] == "replacement-secret"
        assert "authorization" not in _headers(request)
        return httpx.Response(200, json={"reply": None})

    client = _client(handler)
    monkeypatch.setattr(broker_vm, "get_broker_client", lambda: client)

    issued = await post_auth_token(None)
    assert _TOKEN not in issued
    assert json.loads(issued)["reply"]["token_issued"] is True

    reset = json.loads(await post_auth_reset_initial_password(None, "replacement-secret"))
    assert reset["password_updated_for_this_process"] is True
    assert client.password == "replacement-secret"
    assert client._token is None

    await client.login()
    assert json.loads(seen[-1].content) == {"password": "replacement-secret"}


@pytest.mark.asyncio
async def test_log_bundle_returns_bytes_and_errors_do_not_echo_the_password(monkeypatch):
    bundle = b"\x1f\x8b\x00log"

    def logs(request: httpx.Request) -> httpx.Response:
        assert _headers(request)["authorization"] == f"Bearer {_TOKEN}"
        return httpx.Response(200, content=bundle)

    def token_then_logs(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/public_api/v1/auth/token":
            return httpx.Response(200, json={"reply": {"api_key": _TOKEN}})
        return logs(request)

    monkeypatch.setattr(broker_vm, "get_broker_client", lambda: _client(token_then_logs))
    payload = json.loads(await post_logs(None))
    assert base64.b64decode(payload["reply"]["content_base64"]) == bundle

    def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/public_api/v1/auth/token":
            return httpx.Response(401, text=f'{{"error":"bad {_FACTORY}"}}')
        raise AssertionError("operation ran without a token")

    monkeypatch.setattr(broker_vm, "get_broker_client", lambda: _client(rejected))
    failure = await post_network_interface(None, "eth0", "dhcp")
    assert _FACTORY not in failure
    assert "[redacted]" in failure


@pytest.mark.asyncio
async def test_missing_broker_settings_do_not_call_the_network(server_settings):
    server_settings(CORTEX_MCP_BROKER_URL=None, CORTEX_MCP_BROKER_FACTORY_PASSWORD=None)
    result = await post_auth_token(None)
    assert "CORTEX_MCP_BROKER_URL" in result
