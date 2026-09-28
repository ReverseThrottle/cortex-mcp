import logging

import httpx
import pytest

from entities.exceptions import (
    PAPIAuthenticationError,
    PAPIConnectionError,
    PAPIServerError,
)
from pkg.client import PAPIClient, _backoff_seconds


class _RetryConfig:
    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries
        self.http_response_error_message_max_size = 1000


def _client(handler, monkeypatch, max_retries: int = 3) -> PAPIClient:
    monkeypatch.setattr("pkg.client.get_config", lambda: _RetryConfig(max_retries))

    async def _no_wait(self, attempt: int) -> None:
        return None

    monkeypatch.setattr(PAPIClient, "_pause_before_retry", _no_wait)
    return PAPIClient(
        "https://api.example.invalid",
        {"Authorization": "tenant-secret", "x-xdr-auth-id": "42"},
        transport=httpx.MockTransport(handler),
    )


def test_backoff_is_capped_full_jitter(monkeypatch):
    monkeypatch.setattr("pkg.client.random.uniform", lambda start, end: end)
    assert _backoff_seconds(0) == 0.5
    assert _backoff_seconds(1) == 1.0
    assert _backoff_seconds(10) == 8.0


@pytest.mark.asyncio
async def test_connection_errors_retry_then_succeed(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("temporary failure")
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, monkeypatch)
    result = await client.request("POST", "/public_api/v1/case/search", json={"request_data": {}})
    await client.aclose()

    assert result == {"ok": True}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_limit_zero_fails_on_the_first_connection_error(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    client = _client(handler, monkeypatch, max_retries=0)
    with pytest.raises(PAPIConnectionError):
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_429_retries_then_succeeds(monkeypatch, caplog):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"reply": {"err_code": 429, "err_msg": "slow down"}})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="pkg.client"):
        result = await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()

    assert result == {"ok": True}
    assert calls["n"] == 3
    assert "HTTP 429" in caplog.text
    assert "retry 1 of 3" in caplog.text


@pytest.mark.asyncio
async def test_503_gives_up_and_reports_status_and_cortex_code(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"err_code": "unavailable"})

    client = _client(handler, monkeypatch, max_retries=2)
    with pytest.raises(PAPIServerError) as exc:
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()

    assert calls["n"] == 3
    assert exc.value.status_code == 503
    assert exc.value.cortex_error_code == "unavailable"
    assert "HTTP 503" in str(exc.value)
    assert "Cortex error code unavailable" in str(exc.value)


@pytest.mark.asyncio
async def test_401_is_not_retried_and_reports_the_cortex_code(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"reply": {"err_code": 401, "err_msg": "unauthorized"}})

    client = _client(handler, monkeypatch)
    with pytest.raises(PAPIAuthenticationError) as exc:
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()

    assert calls["n"] == 1
    assert exc.value.status_code == 401
    assert exc.value.cortex_error_code == 401
    assert "HTTP 401" in str(exc.value)
    assert "Cortex error code 401" in str(exc.value)


@pytest.mark.asyncio
async def test_500_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    client = _client(handler, monkeypatch)
    with pytest.raises(PAPIServerError) as exc:
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()

    assert calls["n"] == 1
    assert exc.value.status_code == 500
    assert exc.value.cortex_error_code is None
    assert "HTTP 500" in str(exc.value)
    assert "Cortex error code" not in str(exc.value)
