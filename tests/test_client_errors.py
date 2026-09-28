import httpx
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

_STATUS_ERRORS = [
    (401, PAPIAuthenticationError),
    (403, PAPIAuthenticationError),
    (400, PAPIClientRequestError),
    (404, PAPIClientRequestError),
    (429, PAPIClientRequestError),
    (500, PAPIServerError),
    (503, PAPIServerError),
    (302, PAPIResponseError),
]


def _client(handler) -> PAPIClient:
    return PAPIClient(
        "https://api.example.invalid",
        {"Authorization": "tenant-secret", "x-xdr-auth-id": "42"},
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), _STATUS_ERRORS)
async def test_request_maps_http_status_on_a_single_attempt(status, expected):
    handler, state = _counting(httpx.Response(status, text=f"status-{status}"))
    client = _client(handler)
    with pytest.raises(expected, match=str(status) if status not in (401, 403) else "failed"):
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()
    assert state["calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), _STATUS_ERRORS)
async def test_stream_maps_http_status_on_a_single_attempt(status, expected):
    handler, state = _counting(httpx.Response(status, text=f"status-{status}"))
    client = _client(handler)
    with pytest.raises(expected):
        await client.stream("POST", "/public_api/v1/mcp/download/")
    await client.aclose()
    assert state["calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("down"), PAPIConnectionError),
        (httpx.ReadTimeout("slow"), PAPIConnectionError),
        (httpx.RemoteProtocolError("bad protocol"), PAPIConnectionError),
        (RuntimeError("boom"), PAPIClientError),
    ],
)
async def test_request_maps_transport_failures_on_a_single_attempt(error, expected):
    handler, state = _counting(error)
    client = _client(handler)
    with pytest.raises(expected):
        await client.request("POST", "/public_api/v1/case/search")
    await client.aclose()
    assert state["calls"] == 1


@pytest.mark.asyncio
async def test_stream_connection_failure_is_a_single_attempt():
    handler, state = _counting(httpx.ConnectError("down"))
    client = _client(handler)
    with pytest.raises(PAPIConnectionError, match="Failed to connect"):
        await client.stream("POST", "/public_api/v1/mcp/download/")
    await client.aclose()
    assert state["calls"] == 1


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
