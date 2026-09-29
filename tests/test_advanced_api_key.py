import hashlib

import httpx
import pytest

from pkg.client import PAPIClient
from pkg.util import get_papi_auth_headers


def test_standard_key_headers():
    headers = get_papi_auth_headers("my-secret", "10", "standard")
    assert headers == {"Authorization": "my-secret", "X-XDR-AUTH-ID": "10"}


def test_advanced_key_headers_hash_is_correct():
    headers = get_papi_auth_headers("my-secret", "10", "advanced")
    nonce = headers["x-xdr-nonce"]
    timestamp = headers["x-xdr-timestamp"]
    expected_hash = hashlib.sha256(f"my-secret{nonce}{timestamp}".encode()).hexdigest()
    assert headers["Authorization"] == expected_hash
    assert headers["x-xdr-auth-id"] == "10"
    assert len(nonce) == 64
    assert headers["Authorization"] != "my-secret"


def test_advanced_key_headers_are_fresh_per_call():
    first = get_papi_auth_headers("my-secret", "10", "advanced")
    second = get_papi_auth_headers("my-secret", "10", "advanced")
    assert first["x-xdr-nonce"] != second["x-xdr-nonce"]
    assert first["Authorization"] != second["Authorization"]


@pytest.mark.parametrize("key_type", ["standard", "advanced"])
@pytest.mark.asyncio
async def test_send_applies_auth_and_drops_inbound_headers(key_type):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = PAPIClient(
        "https://api.example.invalid",
        "my-secret",
        "10",
        key_type=key_type,
        transport=httpx.MockTransport(handler),
    )
    request = client.build_request(
        "POST",
        "/public_api/v1/system/get_tenant_info",
        headers={
            "Authorization": "Bearer inbound-mcp-token",
            "x-xdr-auth-id": "spoofed",
            "Cookie": "session=abc",
            "X-Custom": "from-client",
            "Content-Type": "application/json",
        },
        json={"request_data": {}},
    )
    await client.send(request)
    await client.send(client.build_request("GET", "/public_api/v1/system/get_tenant_info"))
    await client.aclose()

    assert len(captured) == 2
    first = captured[0].headers
    assert "cookie" not in {key.lower() for key in first.keys()}
    assert "x-custom" not in {key.lower() for key in first.keys()}
    assert first["content-type"].startswith("application/json")
    if key_type == "standard":
        assert first["Authorization"] == "my-secret"
        assert first["X-XDR-AUTH-ID"] == "10"
    else:
        assert first["Authorization"] != "my-secret"
        expected_hash = hashlib.sha256(
            f"my-secret{first['x-xdr-nonce']}{first['x-xdr-timestamp']}".encode()
        ).hexdigest()
        assert first["Authorization"] == expected_hash
        assert captured[0].headers["x-xdr-nonce"] != captured[1].headers["x-xdr-nonce"]


@pytest.mark.asyncio
async def test_retry_uses_a_fresh_advanced_nonce(monkeypatch):
    captured: list[httpx.Request] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"reply": {"err_code": 429, "err_msg": "slow down"}})
        return httpx.Response(200, json={"ok": True})

    async def _no_wait(self, attempt: int) -> None:
        return None

    monkeypatch.setattr(PAPIClient, "_pause_before_retry", _no_wait)
    client = PAPIClient(
        "https://api.example.invalid",
        "my-secret",
        "10",
        key_type="advanced",
        transport=httpx.MockTransport(handler),
    )
    result = await client.request("POST", "/public_api/v1/system/get_tenant_info", json={"request_data": {}})
    await client.aclose()

    assert result == {"ok": True}
    assert len(captured) == 2
    first, second = captured
    assert first.headers["x-xdr-nonce"] != second.headers["x-xdr-nonce"]
    assert first.headers["authorization"] != second.headers["authorization"]
    for request in captured:
        nonce = request.headers["x-xdr-nonce"]
        timestamp = request.headers["x-xdr-timestamp"]
        expected_hash = hashlib.sha256(f"my-secret{nonce}{timestamp}".encode()).hexdigest()
        assert request.headers["authorization"] == expected_hash
        assert request.headers["authorization"] != "my-secret"
