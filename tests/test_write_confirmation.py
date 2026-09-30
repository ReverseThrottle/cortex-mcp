import base64
import json
import os
from typing import Any, cast

import pytest
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import InputRequiredToolResult
from mcp.types import CallToolRequestParams, ElicitResult

from config.config import reload_config
from pkg.write_confirmation import (
    WriteConfirmation,
    WriteConfirmationMiddleware,
    changes_tenant_state,
)
from usecase.builtin_components import xsoar_actions
from usecase.builtin_components.xsoar_actions import (
    _file_bytes,
    post_entry_upload_by_incident_id,
    post_incident_upload_by_incident_id,
)


class _Tool:
    def __init__(self, description: str):
        self.description = description


class _Session:
    def __init__(self, supports: bool):
        self.supports = supports

    def check_client_capability(self, capability):
        return self.supports


class _Server:
    def __init__(self, description: str):
        self.description = description

    async def get_tool(self, key: str):
        return _Tool(self.description)


class _Context:
    def __init__(self, description: str, supports: bool, confirm: bool | None, action: str = "accept"):
        self.fastmcp = _Server(description)
        self.session = _Session(supports)
        self.request_context = object()
        self.elicited = False
        self._confirm = confirm
        self._action = action

    async def elicit(self, message: str, response_type):
        self.elicited = True
        assert response_type is WriteConfirmation
        assert "changes Cortex tenant state" in message
        if self._action == "decline":
            return DeclinedElicitation()
        return AcceptedElicitation(data=WriteConfirmation(confirm=bool(self._confirm)))


def _call_context(description: str, supports: bool = True, confirm: bool | None = True, action: str = "accept"):
    ctx = _Context(description, supports, confirm, action)
    context = MiddlewareContext(
        message=CallToolRequestParams(name="update_case", arguments={}),
        fastmcp_context=cast(Any, ctx),
        method="tools/call",
        type="request",
    )
    return context, ctx


@pytest.fixture
def elicitation(monkeypatch):
    previous = os.environ.get("MCP_ELICITATION_ENABLED")

    def apply(enabled: bool):
        monkeypatch.setenv("MCP_ELICITATION_ENABLED", "true" if enabled else "false")
        return reload_config()

    yield apply

    if previous is None:
        monkeypatch.delenv("MCP_ELICITATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MCP_ELICITATION_ENABLED", previous)
    reload_config()


def test_state_change_detection_ignores_read_only_labels():
    assert changes_tenant_state("Side effects: this operation changes Cortex tenant state")
    assert changes_tenant_state("Side effects: this operation creates or replaces a script")
    assert not changes_tenant_state("Side effects: none. This is a read-only Cortex API call")


@pytest.mark.asyncio
async def test_elicitation_disabled_runs_mutating_tools_without_a_prompt(elicitation):
    elicitation(False)
    context, ctx = _call_context("Side effects: this operation changes tenant state")
    called = {}

    async def call_next(seen):
        called["ran"] = True
        return "ok"

    result = await WriteConfirmationMiddleware().on_call_tool(context, call_next)
    assert result == "ok"
    assert called["ran"] is True
    assert ctx.elicited is False


@pytest.mark.asyncio
async def test_elicitation_confirms_a_write_and_skips_a_lookup(elicitation):
    elicitation(True)
    middleware = WriteConfirmationMiddleware()

    read_context, read_ctx = _call_context("Side effects: none. This is a read-only Cortex API call", supports=False)
    called = {}

    async def call_next(seen):
        called["ran"] = True
        return "read"

    assert await middleware.on_call_tool(read_context, call_next) == "read"
    assert read_ctx.elicited is False

    write_context, write_ctx = _call_context("Side effects: this operation creates a script", confirm=True)

    async def call_write(seen):
        called["write"] = True
        return "wrote"

    assert await middleware.on_call_tool(write_context, call_write) == "wrote"
    assert write_ctx.elicited is True
    assert called["write"] is True


@pytest.mark.asyncio
async def test_elicitation_does_not_run_a_write_without_confirmation(elicitation):
    elicitation(True)
    middleware = WriteConfirmationMiddleware()
    description = "Side effects: this operation changes Cortex tenant state"

    async def call_next(seen):
        raise AssertionError("the tool ran")

    declined, _ = _call_context(description, confirm=False)
    refused = await middleware.on_call_tool(declined, call_next)
    assert refused.is_error is True
    assert "not confirmed" in refused.content[0].text

    cancelled, _ = _call_context(description, action="decline")
    refused = await middleware.on_call_tool(cancelled, call_next)
    assert "not confirmed" in refused.content[0].text

    unsupported, _ = _call_context(description, supports=False)
    refused = await middleware.on_call_tool(unsupported, call_next)
    assert refused.is_error is True
    assert "does not support" in refused.content[0].text


class _ModernContext:
    def __init__(self, responses=None, supports: bool | None = None):
        self.fastmcp = _Server("Side effects: this operation changes tenant state")
        self.request_context = object()
        self.elicited = False
        self.input_responses = responses
        self._supports = supports

    def _is_modern_protocol(self) -> bool:
        return True

    @property
    def session(self):
        if self._supports is None:
            raise RuntimeError("no session")
        return _Session(self._supports)

    async def elicit(self, message: str, response_type):
        raise AssertionError("modern protocol must not use the elicitation back-channel")


def _modern_context(responses=None, supports: bool | None = None):
    ctx = _ModernContext(responses, supports)
    context = MiddlewareContext(
        message=CallToolRequestParams(name="update_case", arguments={}),
        fastmcp_context=cast(Any, ctx),
        method="tools/call",
        type="request",
    )
    return context, ctx


@pytest.mark.asyncio
async def test_modern_protocol_asks_then_honors_the_retried_confirmation(elicitation):
    elicitation(True)
    middleware = WriteConfirmationMiddleware()

    async def call_next(seen):
        return "wrote"

    asked, ctx = _modern_context()
    pending = await middleware.on_call_tool(asked, call_next)
    assert isinstance(pending, InputRequiredToolResult)
    assert ctx.elicited is False

    accepted = ElicitResult(action="accept", content={"confirm": True})
    confirmed, _ = _modern_context({"confirm_write": accepted})
    assert await middleware.on_call_tool(confirmed, call_next) == "wrote"

    declined = ElicitResult(action="decline")
    refused_context, _ = _modern_context({"confirm_write": declined})

    async def must_not_run(seen):
        raise AssertionError("the tool ran")

    refused = await middleware.on_call_tool(refused_context, must_not_run)
    assert refused.is_error is True
    assert "not confirmed" in refused.content[0].text

    blocked, _ = _modern_context(supports=False)
    refused = await middleware.on_call_tool(blocked, must_not_run)
    assert refused.is_error is True
    assert "does not support" in refused.content[0].text


def test_binary_upload_accepts_base64_and_text_still_rejects_nul():
    raw = b"PK\x03\x04\x00\x00zip"
    payload, error = _file_bytes(base64.b64encode(raw).decode(), "base64", "File content")
    assert error is None
    assert payload == raw
    text, error = _file_bytes("note", "utf-8", "File content")
    assert error is None and text == b"note"
    payload, error = _file_bytes("a\x00b", "utf-8", "File content")
    assert payload is None
    assert "binary" in error


@pytest.mark.asyncio
async def test_incident_and_war_room_uploads_send_binary_bytes(monkeypatch):
    captured = []

    class FakeFetcher:
        async def send_request(self, path, **kwargs):
            captured.append((path, kwargs["files"]["file"][1]))
            return {"reply": "ok"}

    async def fake_get_fetcher(ctx):
        return FakeFetcher()

    monkeypatch.setattr(xsoar_actions, "get_fetcher", fake_get_fetcher)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00").decode()
    war_room = await post_entry_upload_by_incident_id(None, "42", encoded, content_encoding="base64")
    incident = await post_incident_upload_by_incident_id(None, "42", encoded, content_encoding="base64")
    assert json.loads(war_room)["reply"] == "ok"
    assert json.loads(incident)["reply"] == "ok"
    assert captured[0][0] == "/xsoar/public/v1/entry/upload/42"
    assert captured[1][0] == "/xsoar/public/v1/incident/upload/42"
    assert captured[0][1] == captured[1][1] == b"\x89PNG\r\n\x1a\n\x00\x00"
