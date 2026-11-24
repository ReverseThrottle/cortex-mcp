"""Coverage for the MCP resource handlers get_cases_response / get_issues_response.

Both handlers (src/usecase/builtin_components/cases.py,
src/usecase/builtin_components/issues.py) wrap pkg.util.read_resource +
json.loads in a try/except with four branches: success, FileNotFoundError,
json.JSONDecodeError, and a generic Exception fallback. These were reachable
MCP resources with 0% coverage; this file exercises every branch for both
handlers so a regression in either can't ship unnoticed.
"""

import json

import pytest

from pkg.util import RESOURCES_DIR
from usecase.builtin_components.cases import get_cases_response
from usecase.builtin_components.issues import get_issues_response

# (module path for monkeypatch targets, handler under test, resource filename)
_CASES = ("usecase.builtin_components.cases", get_cases_response, "cases_response.json")
_ISSUES = ("usecase.builtin_components.issues", get_issues_response, "issues_response.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("module_path, handler, filename", [_CASES, _ISSUES])
async def test_happy_path_round_trips_the_resource_file(module_path, handler, filename):
    on_disk = json.loads((RESOURCES_DIR / filename).read_text())

    result = await handler()
    payload = json.loads(result)

    assert payload["success"] == "true"
    assert payload["response"] == on_disk


@pytest.mark.asyncio
@pytest.mark.parametrize("module_path, handler, filename", [_CASES, _ISSUES])
async def test_file_not_found_returns_an_error_response_instead_of_raising(monkeypatch, module_path, handler, filename):
    def fake_read_resource(_name):
        raise FileNotFoundError(f"Resource file not found: {filename}")

    monkeypatch.setattr(f"{module_path}.read_resource", fake_read_resource)

    result = await handler()
    payload = json.loads(result)

    assert payload["success"] == "false"
    assert filename in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("module_path, handler, filename", [_CASES, _ISSUES])
async def test_invalid_json_returns_an_error_response_instead_of_raising(monkeypatch, module_path, handler, filename):
    monkeypatch.setattr(f"{module_path}.read_resource", lambda _name: "{not valid json")

    result = await handler()
    payload = json.loads(result)

    assert payload["success"] == "false"
    assert "error" in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("module_path, handler, filename", [_CASES, _ISSUES])
async def test_unexpected_error_is_caught_by_the_generic_handler(monkeypatch, module_path, handler, filename):
    def fake_read_resource(_name):
        raise PermissionError("Access denied to resource file")

    monkeypatch.setattr(f"{module_path}.read_resource", fake_read_resource)

    result = await handler()
    payload = json.loads(result)

    assert payload["success"] == "false"
    assert "Access denied" in payload["error"]
