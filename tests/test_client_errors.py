import httpx2 as httpx
import pytest

from entities.exceptions import (
    PAPIAuthenticationError,
    PAPIClientError,
    PAPIClientRequestError,
    PAPIConnectionError,
    PAPIResponseError,
    PAPIServerError,
)
from pkg.client import PAPIClient

# Settings.max_retries defaults to 3 extra attempts (4 calls) for 429, 503, and connection errors.
_DEFAULT_RETRY_CALLS = 4

_STATUS_ERRORS = [
    (401, PAPIAuthenticationError, 1),
    (403, PAPIAuthenticationError, 1),
    (400, PAPIClientRequestError, 1),
    (404, PAPIClientRequestError, 1),
    (429, PAPIClientRequestError, _DEFAULT_RETRY_CALLS),
    (500, PAPIServerError, 1),
    (503, PAPIServerError, _DEFAULT_RETRY_CALLS),
    (302, PAPIResponseError, 1),
]


def _client(handler) -> PAPIClient:
    return PAPIClient(
        "https://api.example.invalid",
        "tenant-secret",
        "42",
        transport=httpx.MockTransport(handler),
    )


def _counting(response):
    state = {"calls": 0}

    def handler(request: httpx.Request):
        state["calls"] += 1
        if isinstance(response, Exception):
            raise response
        return response

    return handler, state


@pytest.fixture(autouse=True)
def _stub_retry_pause(monkeypatch):
    async def _no_wait(self, attempt: int) -> None:
        return None

    monkeypatch.setattr(PAPIClient, "_pause_before_retry", _no_wait)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected", "calls"), _STATUS_ERRORS)
async def test_request_maps_http_status(status, expected, calls):
    handler, state = _counting(httpx.Response(status, text=f"status-{status}"))
    client = _client(handler)
    with pytest.raises(expected, match=str(status) if status not in (401, 403) else "failed"):
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()
    assert state["calls"] == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected", "calls"), _STATUS_ERRORS)
async def test_stream_maps_http_status(status, expected, calls):
    handler, state = _counting(httpx.Response(status, text=f"status-{status}"))
    client = _client(handler)
    with pytest.raises(expected):
        await client.stream("POST", "/public_api/v1/mcp/download/")
    await client.aclose()
    assert state["calls"] == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected", "calls"),
    [
        (httpx.ConnectError("down"), PAPIConnectionError, _DEFAULT_RETRY_CALLS),
        (httpx.ReadTimeout("slow"), PAPIConnectionError, _DEFAULT_RETRY_CALLS),
        (httpx.RemoteProtocolError("bad protocol"), PAPIConnectionError, _DEFAULT_RETRY_CALLS),
        (RuntimeError("boom"), PAPIClientError, 1),
    ],
)
async def test_request_maps_transport_failures(error, expected, calls):
    handler, state = _counting(error)
    client = _client(handler)
    with pytest.raises(expected):
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()
    assert state["calls"] == calls


@pytest.mark.asyncio
async def test_stream_connection_failure_retries_then_raises():
    handler, state = _counting(httpx.ConnectError("down"))
    client = _client(handler)
    with pytest.raises(PAPIConnectionError, match="Failed to connect"):
        await client.stream("POST", "/public_api/v1/mcp/download/")
    await client.aclose()
    assert state["calls"] == _DEFAULT_RETRY_CALLS


@pytest.mark.asyncio
async def test_stream_timeout_retries_then_raises():
    handler, state = _counting(httpx.ReadTimeout("slow"))
    client = _client(handler)
    with pytest.raises(PAPIConnectionError, match="Request timeout"):
        await client.stream("POST", "/public_api/v1/mcp/download/")
    await client.aclose()
    assert state["calls"] == _DEFAULT_RETRY_CALLS


@pytest.mark.asyncio
async def test_raw_and_stream_success_return_bytes():
    payload = b"\x1f\x8braw"
    client = _client(lambda request: httpx.Response(200, content=payload))
    assert await client.request("POST", "/public_api/v1/xql/get_query_results_stream", raw=True) == payload
    body = await client.stream("POST", "/public_api/v1/mcp/download/")
    assert body is not None
    assert body.read() == payload
    await client.aclose()


@pytest.mark.asyncio
async def test_none_response_raises_response_error(monkeypatch):
    client = _client(lambda request: httpx.Response(200, json={"ok": True}))

    async def return_none(self, method, url, **kwargs):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "request", return_none)
    with pytest.raises(PAPIResponseError, match="None response"):
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()
