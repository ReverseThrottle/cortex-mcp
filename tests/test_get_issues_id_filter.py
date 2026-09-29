import json

import pytest

from usecase.builtin_components.issues import get_issues


class _Fetcher:
    def __init__(self) -> None:
        self.payload = None

    async def send_request(self, path, data=None, **kwargs):
        self.payload = data
        return {"reply": {"issues": []}}


@pytest.mark.asyncio
async def test_bare_int_id_filter_is_sent_as_a_list(monkeypatch):
    fetcher = _Fetcher()

    async def fake_get_fetcher(ctx):
        return fetcher

    monkeypatch.setattr("usecase.builtin_components.issues.get_fetcher", fake_get_fetcher)
    result = await get_issues(None, filters=[{"field": "id", "operator": "in", "value": 12158}])
    payload = json.loads(result)

    assert payload["success"] == "true"
    assert fetcher.payload["request_data"]["filters"][0]["value"] == [12158]


@pytest.mark.asyncio
async def test_non_numeric_id_filter_returns_an_error_without_calling_the_api(monkeypatch):
    async def fake_get_fetcher(ctx):
        raise AssertionError("invalid id must not call the API")

    monkeypatch.setattr("usecase.builtin_components.issues.get_fetcher", fake_get_fetcher)
    result = await get_issues(None, filters=[{"field": "id", "operator": "in", "value": "not-a-number"}])
    payload = json.loads(result)

    assert payload["success"] == "false"
    assert "Invalid 'id' filter value" in payload["error"]
    assert "not iterable" not in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, 1.9, [12158, 1.9], [True]])
async def test_bool_and_fractional_id_filters_are_rejected(monkeypatch, value):
    async def fake_get_fetcher(ctx):
        raise AssertionError("invalid id must not call the API")

    monkeypatch.setattr("usecase.builtin_components.issues.get_fetcher", fake_get_fetcher)
    result = await get_issues(None, filters=[{"field": "id", "operator": "in", "value": value}])
    payload = json.loads(result)

    assert payload["success"] == "false"
    assert "Invalid 'id' filter value" in payload["error"]


@pytest.mark.asyncio
async def test_list_of_id_values_is_coerced_to_integers(monkeypatch):
    fetcher = _Fetcher()

    async def fake_get_fetcher(ctx):
        return fetcher

    monkeypatch.setattr("usecase.builtin_components.issues.get_fetcher", fake_get_fetcher)
    result = await get_issues(None, filters=[{"field": "id", "operator": "in", "value": [12158, "999"]}])
    payload = json.loads(result)

    assert payload["success"] == "true"
    assert fetcher.payload["request_data"]["filters"][0]["value"] == [12158, 999]
