"""Tests for the real Fetcher dispatch layer (issue #62).

Every tool test installs a fake fetcher, so Fetcher.send_request, get_fetcher,
and the update CLI's download success path were never executed. These tests
drive the REAL Fetcher and PAPIClient against httpx.MockTransport (the pattern
from tests/test_client_errors.py), injecting the transport into the client
Fetcher builds internally instead of replacing the client class.
"""

import io
import json
import os
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from config.config import reload_config
from entities.MCPContext import MCPContext
from pkg.client import PAPIClient
from usecase.fetcher import Fetcher, get_fetcher

_ENV_KEYS = (
    "CORTEX_MCP_PAPI_URL",
    "CORTEX_MCP_PAPI_AUTH_HEADER",
    "CORTEX_MCP_PAPI_AUTH_ID",
    "CORTEX_MCP_PAPI_KEY_TYPE",
)


@pytest.fixture
def papi_env():
    """Settings credentials distinct from any lifespan headers, restored after."""
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ.update(
        {
            "CORTEX_MCP_PAPI_URL": "https://tenant.example",
            "CORTEX_MCP_PAPI_AUTH_HEADER": "settings-secret",
            "CORTEX_MCP_PAPI_AUTH_ID": "99",
            "CORTEX_MCP_PAPI_KEY_TYPE": "standard",
        }
    )
    reload_config()
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reload_config()


def _install_mock_transport(monkeypatch, handler):
    """Fetcher constructs its own PAPIClient; inject a MockTransport through
    __init__ so the real request/auth/retry path still runs end to end."""
    original_init = PAPIClient.__init__

    def init_with_transport(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(PAPIClient, "__init__", init_with_transport)


def _recording(response):
    captured = {}

    def handler(request: httpx.Request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        captured["extensions"] = dict(request.extensions)
        return response

    return handler, captured


def _fetcher(**overrides) -> Fetcher:
    params = {"url": "https://api-tenant.example", "api_key": "tenant-secret", "api_key_id": "42"}
    params.update(overrides)
    return Fetcher(**params)


# --- Fetcher.send_request: prefix handling and dispatch modes ---


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["case/search", "/case/search"])
async def test_send_request_adds_papi_prefix_exactly_once(monkeypatch, path):
    handler, captured = _recording(httpx.Response(200, json={"reply": {"ok": True}}))
    _install_mock_transport(monkeypatch, handler)

    result = await _fetcher().send_request(path, data={"request_data": {"limit": 1}})

    assert result == {"reply": {"ok": True}}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api-tenant.example/public_api/v1/case/search"
    assert json.loads(captured["content"]) == {"request_data": {"limit": 1}}
    # The Fetcher's credentials reach the wire as standard-key auth headers.
    assert captured["headers"]["authorization"] == "tenant-secret"
    assert captured["headers"]["x-xdr-auth-id"] == "42"


@pytest.mark.asyncio
async def test_send_request_omit_papi_prefix_skips_the_prefix(monkeypatch):
    handler, captured = _recording(httpx.Response(200, json={"reply": {}}))
    _install_mock_transport(monkeypatch, handler)

    await _fetcher().send_request("/healthcheck", omit_papi_prefix=True)

    assert captured["url"] == "https://api-tenant.example/healthcheck"
    # An empty write still gets a JSON object body (PAPIClient._ensure_json_body).
    assert captured["content"] == b"{}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/public_api/v1/case/search", "/public_api/v1/mcp/download/"])
async def test_send_request_does_not_double_prefix_an_already_prefixed_path(monkeypatch, path):
    handler, captured = _recording(httpx.Response(200, json={"reply": {}}))
    _install_mock_transport(monkeypatch, handler)

    await _fetcher().send_request(path)

    assert captured["url"] == f"https://api-tenant.example{path}"


@pytest.mark.asyncio
async def test_send_request_raw_returns_the_original_bytes(monkeypatch):
    # Gzip magic but not valid gzip: raw=True must observe the body untouched
    # instead of gunzipping or rejecting non-UTF-8 bytes.
    payload = b"\x1f\x8b\x08\x00not-really-gzip"
    handler, captured = _recording(
        httpx.Response(200, content=payload, headers={"Content-Type": "application/octet-stream"})
    )
    _install_mock_transport(monkeypatch, handler)

    result = await _fetcher().send_request("xql/get_query_results_stream", data={"query_id": "7"}, raw=True)

    assert isinstance(result, bytes)
    assert result == payload
    assert json.loads(captured["content"]) == {"query_id": "7"}


@pytest.mark.asyncio
async def test_send_request_files_sends_multipart(monkeypatch):
    handler, captured = _recording(httpx.Response(200, json={"reply": {"success": True}}))
    _install_mock_transport(monkeypatch, handler)
    payload = b"PK\x03\x04zip-body"

    # Production upload callers (content_uploads, xsoar_actions) send files only.
    result = await _fetcher().send_request(
        "scripts/insert",
        files={"file": ("script.yml.zip", payload, "application/zip")},
        omit_papi_prefix=True,
    )

    assert result == {"reply": {"success": True}}
    assert captured["url"] == "https://api-tenant.example/scripts/insert"
    assert captured["headers"]["content-type"].startswith("multipart/form-data")
    assert b"script.yml.zip" in captured["content"]
    assert payload in captured["content"]
    assert captured["headers"]["authorization"] == "tenant-secret"


@pytest.mark.asyncio
async def test_send_request_stream_returns_a_rewound_bytesio(monkeypatch):
    payload = b"PK\x03\x04update-package"
    handler, captured = _recording(httpx.Response(200, content=payload))
    _install_mock_transport(monkeypatch, handler)

    result = await _fetcher().send_request(
        "/public_api/v1/mcp/download/",
        data=json.dumps({"request_data": {"is_update": True}}),
        stream=True,
    )

    assert isinstance(result, io.BytesIO)
    assert result.read() == payload
    assert json.loads(captured["content"]) == {"request_data": {"is_update": True}}
    assert captured["headers"]["content-type"] == "application/zip"


@pytest.mark.asyncio
async def test_send_request_advanced_key_signs_the_request(monkeypatch):
    handler, captured = _recording(httpx.Response(200, json={"reply": {}}))
    _install_mock_transport(monkeypatch, handler)

    await _fetcher(key_type="advanced").send_request("case/search")

    headers = captured["headers"]
    assert "x-xdr-nonce" in headers
    assert "x-xdr-timestamp" in headers
    # Advanced keys send SHA256(api_key + nonce + timestamp), not the raw key.
    assert headers["authorization"] != "tenant-secret"
    assert len(headers["authorization"]) == 64


@pytest.mark.asyncio
async def test_send_request_timeout_caps_the_attempt(monkeypatch):
    handler, captured = _recording(httpx.Response(200, json={"reply": {}}))
    _install_mock_transport(monkeypatch, handler)

    await _fetcher().send_request("case/search", timeout=5.0)

    assert captured["extensions"]["timeout"]["read"] == 5.0


# --- get_fetcher: credential resolution and context state ---


class _FakeContext:
    """Minimal fastmcp.Context stand-in: request_context plus async set_state."""

    def __init__(self, auth_headers=None, *, with_request_context=True):
        self.request_context = (
            SimpleNamespace(lifespan_context=MCPContext(auth_headers=auth_headers or {}))
            if with_request_context
            else None
        )
        self.state = {}
        self.serializable = {}

    async def set_state(self, key, value, *, serializable=True):
        self.state[key] = value
        self.serializable[key] = serializable


@pytest.mark.asyncio
async def test_get_fetcher_prefers_lifespan_headers_and_stores_the_fetcher(papi_env):
    ctx = _FakeContext({"Authorization": "lifespan-key", "X-XDR-AUTH-ID": "7"})

    fetcher = await get_fetcher(ctx)

    assert fetcher.url == "https://api-tenant.example"
    assert fetcher.api_key == "lifespan-key"
    assert fetcher.api_key_id == "7"
    assert fetcher.key_type == "standard"
    assert ctx.state["fetcher"] is fetcher
    assert ctx.serializable["fetcher"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_headers",
    [{}, {"Authorization": "only-key"}, {"X-XDR-AUTH-ID": "7"}],
    ids=["no-headers", "authorization-only", "auth-id-only"],
)
async def test_get_fetcher_falls_back_to_settings_when_either_header_is_missing(papi_env, auth_headers):
    # `if not (api_key and xdr_id)`: partial lifespan headers fall back too.
    ctx = _FakeContext(auth_headers)

    fetcher = await get_fetcher(ctx)

    assert fetcher.api_key == "settings-secret"
    assert fetcher.api_key_id == "99"
    assert ctx.state["fetcher"] is fetcher


@pytest.mark.asyncio
async def test_get_fetcher_raises_without_a_request_context(papi_env):
    with pytest.raises(RuntimeError, match="MCP request context is unavailable"):
        await get_fetcher(_FakeContext(with_request_context=False))


@pytest.mark.asyncio
async def test_get_fetcher_uses_the_settings_key_type(papi_env):
    os.environ["CORTEX_MCP_PAPI_KEY_TYPE"] = "advanced"
    reload_config()

    fetcher = await get_fetcher(_FakeContext({"Authorization": "lifespan-key", "X-XDR-AUTH-ID": "7"}))

    assert fetcher.key_type == "advanced"


@pytest.mark.asyncio
async def test_get_fetcher_normalizes_the_settings_url(papi_env):
    os.environ["CORTEX_MCP_PAPI_URL"] = "tenant.example"
    reload_config()

    fetcher = await get_fetcher(_FakeContext({"Authorization": "lifespan-key", "X-XDR-AUTH-ID": "7"}))

    assert fetcher.url == "https://api-tenant.example"
