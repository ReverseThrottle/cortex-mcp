"""
Regression test for a real bug found during live-tenant validation: get_issues crashed
with an unhandled TypeError ('int' object is not iterable) when a caller passed a bare
int as an "id" filter value instead of a list, e.g. {"field": "id", "value": 12158}
instead of {"field": "id", "value": [12158]} — despite the tool's own docstring showing
the list form as the example. The crash happened before the try/except boundary, so it
surfaced as a raw ToolError instead of a graceful create_response(is_error=True) payload.
"""

import json

from fastmcp import Client


async def test_bare_int_id_filter_is_coerced_not_crashed(mcp_server):
    """A bare int must not crash the tool — it should be coerced into a list and proceed
    (failing later only on the network call to a nonexistent test host, not on a TypeError)."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_issues", {"filters": [{"field": "id", "operator": "in", "value": 12158}], "search_to": 1}
        )
    payload = json.loads(result.content[0].text)
    assert payload["success"] == "false"
    assert "not iterable" not in payload["error"]


async def test_non_numeric_id_filter_value_fails_gracefully(mcp_server):
    """A genuinely invalid id value should return a clear error, not crash."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_issues", {"filters": [{"field": "id", "operator": "in", "value": "not-a-number"}], "search_to": 1}
        )
    payload = json.loads(result.content[0].text)
    assert payload["success"] == "false"
    assert "Invalid 'id' filter value" in payload["error"]


async def test_list_of_ids_still_works(mcp_server):
    """The originally-documented list form must keep working."""
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "get_issues", {"filters": [{"field": "id", "operator": "in", "value": [12158, 999]}], "search_to": 1}
        )
    payload = json.loads(result.content[0].text)
    assert payload["success"] == "false"
    assert "not iterable" not in payload["error"]
    assert "Invalid 'id' filter value" not in payload["error"]
