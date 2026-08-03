"""
Verifies MCP_WRITE_TOOLS_ENABLED / MCP_ISOLATE_ENDPOINT_TOOL_ENABLED actually gate their
tools. Regression test for a real bug found during this build-out: update_case, isolate_endpoint,
and unisolate_endpoint were registered unconditionally with no runtime check of their flags at
all, contradicting the README's "disabled by default" claim. update_issue is covered here too
since it uses the same require_flag decorator.
"""

import json

import pytest
from fastmcp import Client


@pytest.mark.parametrize(
    "tool_name,args,flag_env",
    [
        ("update_case", {"case_ids": [1], "comment": "test"}, "MCP_WRITE_TOOLS_ENABLED"),
        ("update_issue", {"issue_ids": ["1"], "comment": "test"}, "MCP_WRITE_TOOLS_ENABLED"),
        ("isolate_endpoint", {"endpoint_ids": ["abc"]}, "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED"),
        ("unisolate_endpoint", {"endpoint_ids": ["abc"]}, "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED"),
    ],
)
async def test_tool_refuses_when_flag_disabled(mcp_server, flags, tool_name, args, flag_env):
    flags(**{flag_env: "false"})
    async with Client(mcp_server) as client:
        result = await client.call_tool(tool_name, args)
    payload = json.loads(result.content[0].text)
    assert payload["success"] == "false"
    assert "disabled" in payload["error"].lower()


@pytest.mark.parametrize(
    "tool_name,args,flag_env",
    [
        ("update_case", {"case_ids": [1], "comment": "test"}, "MCP_WRITE_TOOLS_ENABLED"),
        ("update_issue", {"issue_ids": ["1"], "comment": "test"}, "MCP_WRITE_TOOLS_ENABLED"),
        ("isolate_endpoint", {"endpoint_ids": ["abc"]}, "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED"),
        ("unisolate_endpoint", {"endpoint_ids": ["abc"]}, "MCP_ISOLATE_ENDPOINT_TOOL_ENABLED"),
    ],
)
async def test_tool_proceeds_when_flag_enabled(mcp_server, flags, tool_name, args, flag_env):
    """When enabled, the tool should get past the gate and attempt the real network call
    (which fails here only because the test host doesn't resolve — proving the gate, not
    the network, is what blocked it in the disabled case above)."""
    flags(**{flag_env: "true"})
    async with Client(mcp_server) as client:
        result = await client.call_tool(tool_name, args)
    payload = json.loads(result.content[0].text)
    assert payload["success"] == "false"
    assert "disabled" not in payload["error"].lower()
