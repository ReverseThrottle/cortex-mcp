"""
Regression tests for Cortex "Standard" vs "Advanced" API key authentication.

Found while validating against a real tenant: the server only implemented Standard-key
auth (raw key + ID headers) and had no way to authenticate against an Advanced-key tenant
at all — every request came back 401. Advanced keys require a fresh SHA256(key + nonce +
timestamp) computed per request; a header set baked in once at client construction time
(the previous PAPIClient design) can't support that even after adding the hashing logic,
since FastMCP's OpenAPI-generated tools call the underlying httpx client's send() directly,
bypassing PAPIClient.request(). These tests cover both the header computation and that the
per-request event hook actually reaches every outgoing request regardless of caller.
"""

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


def test_advanced_key_headers_are_fresh_per_call():
    first = get_papi_auth_headers("my-secret", "10", "advanced")
    second = get_papi_auth_headers("my-secret", "10", "advanced")
    assert first["x-xdr-nonce"] != second["x-xdr-nonce"]
    assert first["Authorization"] != second["Authorization"]


@pytest.mark.parametrize("key_type", ["standard", "advanced"])
async def test_papi_client_applies_auth_headers_to_every_outgoing_request(key_type):
    """The request event hook must fire regardless of whether the caller goes through
    PAPIClient.request() or calls send() directly (as FastMCP's OpenAPI tools do)."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = PAPIClient(
        "https://api-test.xdr.us.paloaltonetworks.com",
        "my-secret",
        "10",
        key_type=key_type,
        transport=httpx.MockTransport(handler),
    )
    async with client:
        # Simulate FastMCP's OpenAPI tool path: build a raw Request and call send() directly,
        # bypassing PAPIClient.request() entirely.
        request = client.build_request("GET", "/public_api/v1/system/get_tenant_info")
        await client.send(request)

    assert len(captured) == 1
    sent_headers = captured[0].headers
    if key_type == "standard":
        assert sent_headers["Authorization"] == "my-secret"
        assert sent_headers["X-XDR-AUTH-ID"] == "10"
    else:
        assert "x-xdr-nonce" in sent_headers
        assert "x-xdr-timestamp" in sent_headers
        expected_hash = hashlib.sha256(
            f"my-secret{sent_headers['x-xdr-nonce']}{sent_headers['x-xdr-timestamp']}".encode()
        ).hexdigest()
        assert sent_headers["Authorization"] == expected_hash


async def test_papi_client_never_sends_raw_advanced_key_as_authorization():
    """Sanity check for the exact bug found: an advanced key must never appear in the
    Authorization header verbatim, only as the SHA256 hash."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = PAPIClient(
        "https://api-test.xdr.us.paloaltonetworks.com",
        "my-secret",
        "10",
        key_type="advanced",
        transport=httpx.MockTransport(handler),
    )
    async with client:
        await client.request("POST", "/public_api/v1/system/get_tenant_info", json={"request_data": {}})

    assert captured[0].headers["Authorization"] != "my-secret"
