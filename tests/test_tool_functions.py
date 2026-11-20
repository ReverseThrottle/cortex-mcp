import gzip
import json
import zipfile
from io import BytesIO

import jsonschema
import pytest

from config.config import get_config
from entities.exceptions import PAPIAuthenticationError, PAPIConnectionError
from pkg.util import bundle_openapi_from_folders
from usecase.builtin_components import cases as cases_module
from usecase.builtin_components import content_uploads as uploads_module
from usecase.builtin_components import issues as issues_module
from usecase.builtin_components import xsoar_actions as xsoar_module
from usecase.builtin_components.cases import get_cases
from usecase.builtin_components.content_uploads import insert_playbook, insert_script
from usecase.builtin_components.issues import get_issues
from usecase.builtin_components.xsoar_actions import (
    post_entry_upload_by_incident_id,
    post_incident_upload_by_incident_id,
    post_inv_playbook_task_complete,
    post_playbook_save_yaml,
    put_settings_credentials,
)
from usecase.custom_components import case_actions as case_actions_module
from usecase.custom_components import endpoint_actions as endpoint_actions_module
from usecase.custom_components import xql_query as xql_module
from usecase.custom_components.case_actions import update_case
from usecase.custom_components.endpoint_actions import (
    isolate_endpoint,
    unisolate_endpoint,
)
from usecase.custom_components.xql_query import (
    post_xql_get_query_results_stream,
    run_xql_query,
)


class RecordingFetcher:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def send_request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _install(monkeypatch, module, fetcher):
    async def fake_get_fetcher(ctx):
        return fetcher

    monkeypatch.setattr(module, "get_fetcher", fake_get_fetcher)
    return fetcher


def _loaded(result: str) -> dict:
    return json.loads(result)


@pytest.mark.asyncio
async def test_get_cases_coerces_ids_and_returns_papi_errors(monkeypatch):
    fetcher = _install(monkeypatch, cases_module, RecordingFetcher({"reply": [{"id": 7}]}))
    filters = [{"field": "id", "operator": "in", "value": ["7"]}]
    payload = _loaded(
        await get_cases(
            None,
            filters,
            search_from=1,
            search_to=5,
            sort={"field": "creation_time", "keyword": "desc"},
        )
    )
    assert payload["success"] == "true"
    assert payload["reply"] == [{"id": 7}]
    assert filters == [{"field": "id", "operator": "in", "value": ["7"]}]
    path, kwargs = fetcher.calls[0]
    assert path == "case/search/"
    request_data = kwargs["data"]["request_data"]
    assert request_data["filters"][0]["value"] == [7]
    assert request_data["search_from"] == 1
    assert request_data["search_to"] == 5
    assert request_data["sort"] == {"field": "creation_time", "keyword": "desc"}

    empty = _install(monkeypatch, cases_module, RecordingFetcher({"reply": []}))
    await get_cases(None, [])
    assert "filters" not in empty.calls[0][1]["data"]["request_data"]

    failed = _install(monkeypatch, cases_module, RecordingFetcher(PAPIAuthenticationError("denied")))
    error = _loaded(await get_cases(None, []))
    assert error["success"] == "false"
    assert "denied" in error["error"]
    assert failed.calls


@pytest.mark.asyncio
async def test_get_issues_sends_the_search_path(monkeypatch):
    fetcher = _install(monkeypatch, issues_module, RecordingFetcher({"reply": [{"id": 3}]}))
    payload = _loaded(
        await get_issues(None, [{"field": "id", "operator": "in", "value": ["3"]}], search_from=0, search_to=10)
    )
    assert payload["success"] == "true"
    assert payload["reply"] == [{"id": 3}]
    assert "_metadata" not in payload
    assert "formatting_instructions" not in payload
    path, kwargs = fetcher.calls[0]
    assert path == "/issue/search/"
    assert kwargs["data"]["request_data"]["filters"][0]["value"] == [3]

    _install(monkeypatch, issues_module, RecordingFetcher(PAPIConnectionError("offline")))
    error = _loaded(await get_issues(None, []))
    assert error["success"] == "false"
    assert "offline" in error["error"]


@pytest.mark.asyncio
async def test_update_case_validates_before_posting(monkeypatch):
    idle = _install(monkeypatch, case_actions_module, RecordingFetcher())
    missing = _loaded(await update_case(None, [1]))
    assert missing["success"] == "false"
    assert "At least one" in missing["error"]

    invalid_severity = _loaded(await update_case(None, [1], severity="urgent"))
    assert invalid_severity["success"] == "false"
    assert "Invalid severity" in invalid_severity["error"]

    no_reason = _loaded(await update_case(None, [1], status="Resolved"))
    assert no_reason["success"] == "false"
    assert "resolve_reason is required" in no_reason["error"]

    stray_reason = _loaded(await update_case(None, [1], resolve_reason="Resolved - Other"))
    assert stray_reason["success"] == "false"
    assert "only applies when status is 'Resolved'" in stray_reason["error"]

    stray_comment = _loaded(await update_case(None, [1], resolve_comment="done"))
    assert stray_comment["success"] == "false"
    assert "only applies when status is 'Resolved'" in stray_comment["error"]
    assert idle.calls == []


@pytest.mark.asyncio
async def test_update_case_posts_per_case_builtin_fields(monkeypatch):
    fetcher = _install(
        monkeypatch,
        case_actions_module,
        RecordingFetcher({"reply": {"updated": True}}, {"reply": {"updated": True}}),
    )
    updated = _loaded(
        await update_case(
            None,
            [4, 5],
            notes="note",
            status="In Progress",
            severity="high",
            assigned_user="analyst@example.com",
        )
    )
    assert updated["success"] == "true"
    assert [path for path, _ in fetcher.calls] == ["case/update/4", "case/update/5"]
    for _, kwargs in fetcher.calls:
        request_data = kwargs["data"]["request_data"]
        assert set(request_data) == {"update_data"}
        assert request_data["update_data"] == {
            "notes": "note",
            "status_progress": "In Progress",
            "user_severity": "high",
            "assigned_user": "analyst@example.com",
        }
    assert updated["results"] == {"4": {"reply": {"updated": True}}, "5": {"reply": {"updated": True}}}

    _install(monkeypatch, case_actions_module, RecordingFetcher(PAPIAuthenticationError("denied")))
    error = _loaded(await update_case(None, [4], notes="note"))
    assert error["success"] == "false"
    assert "denied" in error["results"]["4"]["error"]


@pytest.mark.asyncio
async def test_update_case_resolving_custom_status_and_clearing_severity(monkeypatch):
    resolving = _install(monkeypatch, case_actions_module, RecordingFetcher({"reply": True}))
    payload = _loaded(
        await update_case(None, [7], status="resolved", resolve_reason="Resolved - Other", resolve_comment="fixed")
    )
    assert payload["success"] == "true"
    assert resolving.calls[0][0] == "case/update/7"
    assert resolving.calls[0][1]["data"]["request_data"]["update_data"] == {
        "status_progress": "resolved",
        "resolve_reason": "Resolved - Other",
        "resolve_comment": "fixed",
    }

    # status_progress is case-insensitive with tenant-custom values: the client
    # must not whitelist statuses (the API lists valid options on error).
    custom = _install(monkeypatch, case_actions_module, RecordingFetcher({"reply": True}))
    result = _loaded(await update_case(None, [8], status="Waiting on Customer"))
    assert result["success"] == "true"
    assert custom.calls[0][1]["data"]["request_data"]["update_data"] == {"status_progress": "Waiting on Customer"}

    # An empty string clears the severity override (spec user_severity semantics).
    cleared = _install(monkeypatch, case_actions_module, RecordingFetcher({"reply": True}))
    result = _loaded(await update_case(None, [9], severity=""))
    assert result["success"] == "true"
    assert cleared.calls[0][1]["data"]["request_data"]["update_data"] == {"user_severity": ""}


@pytest.mark.asyncio
async def test_update_case_reports_per_case_outcomes_on_partial_failure(monkeypatch):
    fetcher = _install(
        monkeypatch, case_actions_module, RecordingFetcher({"reply": "ok"}, PAPIAuthenticationError("denied"))
    )
    result = _loaded(await update_case(None, [4, 5], notes="note"))
    assert result["success"] == "false"
    assert result["results"]["4"] == {"reply": "ok"}
    assert "denied" in result["results"]["5"]["error"]
    assert len(fetcher.calls) == 2


@pytest.mark.asyncio
async def test_update_case_payload_conforms_to_vendor_spec(monkeypatch):
    # Issue #66: update_data allows additionalProperties (custom fields), so a
    # wrong built-in key is silently accepted as a custom field and the intended
    # update never applies. Pin the outgoing path and every wire key to the
    # shipped spec's built-in properties.
    spec = bundle_openapi_from_folders()
    operation = spec["paths"]["/public_api/v1/case/update/{case_id}"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    request_props = schema["properties"]["request_data"]["properties"]
    assert set(request_props) == {"update_data"}  # no case_id_list anywhere in the spec
    builtin_fields = set(request_props["update_data"]["properties"])

    fetcher = _install(monkeypatch, case_actions_module, RecordingFetcher({"reply": True}, {"reply": True}))
    await update_case(
        None,
        [4, 5],
        notes="n",
        status="Resolved",
        resolve_reason="Resolved - Other",
        resolve_comment="c",
        severity="critical",
        assigned_user="analyst@example.com",
    )
    assert len(fetcher.calls) == 2
    for case_id, (path, kwargs) in zip([4, 5], fetcher.calls, strict=True):
        assert path == f"case/update/{case_id}"
        payload = kwargs["data"]
        jsonschema.validate(payload, schema)
        update_data = payload["request_data"]["update_data"]
        assert set(update_data) <= builtin_fields
        assert set(update_data) == {
            "notes",
            "status_progress",
            "resolve_reason",
            "resolve_comment",
            "user_severity",
            "assigned_user",
        }


@pytest.mark.asyncio
async def test_isolate_and_unisolate_post_endpoint_filters(monkeypatch):
    isolate = _install(monkeypatch, endpoint_actions_module, RecordingFetcher({"reply": "isolated"}))
    isolated = _loaded(await isolate_endpoint(None, ["ep-1"], comment="contain"))
    assert isolated["reply"] == "isolated"
    request_data = isolate.calls[0][1]["data"]["request_data"]
    assert isolate.calls[0][0] == "endpoints/isolate/"
    assert request_data["filters"] == [{"field": "endpoint_id_list", "operator": "in", "value": ["ep-1"]}]
    assert request_data["comment"] == "contain"

    restore = _install(monkeypatch, endpoint_actions_module, RecordingFetcher({"reply": "restored"}))
    restored = _loaded(await unisolate_endpoint(None, ["ep-1"]))
    assert restored["success"] == "true"
    restored_data = restore.calls[0][1]["data"]["request_data"]
    assert restore.calls[0][0] == "endpoints/unisolate/"
    assert restored_data["filters"] == [{"field": "endpoint_id_list", "operator": "in", "value": ["ep-1"]}]
    assert "comment" not in restored_data

    _install(monkeypatch, endpoint_actions_module, RecordingFetcher(PAPIConnectionError("offline")))
    error = _loaded(await isolate_endpoint(None, ["ep-1"]))
    assert error["success"] == "false"
    assert "offline" in error["error"]


@pytest.mark.asyncio
async def test_unisolate_endpoint_passes_the_audit_comment_through(monkeypatch):
    # Issue #73: the comment is the audit justification for restoring network
    # access. Lock exact passthrough and absence when omitted, mirroring the
    # isolate expectations in test_isolate_and_unisolate_post_endpoint_filters.
    restore = _install(monkeypatch, endpoint_actions_module, RecordingFetcher({"reply": "restored"}))
    restored = _loaded(await unisolate_endpoint(None, ["ep-1"], comment="cleared"))
    assert restored["success"] == "true"
    assert restore.calls[0][0] == "endpoints/unisolate/"
    request_data = restore.calls[0][1]["data"]["request_data"]
    assert request_data["filters"] == [{"field": "endpoint_id_list", "operator": "in", "value": ["ep-1"]}]
    assert request_data["comment"] == "cleared"

    bare = _install(monkeypatch, endpoint_actions_module, RecordingFetcher({"reply": "restored"}))
    assert _loaded(await unisolate_endpoint(None, ["ep-1"]))["success"] == "true"
    assert "comment" not in bare.calls[0][1]["data"]["request_data"]


@pytest.mark.asyncio
async def test_unisolate_endpoint_papi_error_returns_tool_error(monkeypatch):
    _install(monkeypatch, endpoint_actions_module, RecordingFetcher(PAPIConnectionError("offline")))
    error = _loaded(await unisolate_endpoint(None, ["ep-1"]))
    assert error["success"] == "false"
    assert "offline" in error["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [isolate_endpoint, unisolate_endpoint])
async def test_endpoint_writes_return_tool_error_on_unexpected_exception(monkeypatch, tool):
    # Issue #73: a non-PAPI exception must hit the generic except branch and
    # still return the success="false" tool-error contract, not propagate.
    _install(monkeypatch, endpoint_actions_module, RecordingFetcher(RuntimeError("boom")))
    error = _loaded(await tool(None, ["ep-1"]))
    assert error["success"] == "false"
    assert "boom" in error["error"]


@pytest.mark.asyncio
async def test_endpoint_and_case_writes_reject_an_empty_target_list(monkeypatch):
    endpoints = _install(monkeypatch, endpoint_actions_module, RecordingFetcher())
    for tool in (isolate_endpoint, unisolate_endpoint):
        rejected = _loaded(await tool(None, [], comment="contain"))
        assert rejected["success"] == "false"
        assert rejected["error"] == "endpoint_ids must contain at least one endpoint ID."
    assert endpoints.calls == []

    cases = _install(monkeypatch, case_actions_module, RecordingFetcher())
    rejected = _loaded(await update_case(None, [], notes="note"))
    assert rejected["success"] == "false"
    assert rejected["error"] == "case_ids must contain at least one case ID."
    assert cases.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [post_entry_upload_by_incident_id, post_incident_upload_by_incident_id])
@pytest.mark.parametrize("incident_id", ["1/../../x", "1?x", "1#x", "..", "1%2F"])
async def test_xsoar_uploads_reject_incident_ids_that_leave_the_path(monkeypatch, tool, incident_id):
    idle = _install(monkeypatch, xsoar_module, RecordingFetcher())
    rejected = _loaded(await tool(None, incident_id, "note"))
    assert rejected["success"] == "false"
    assert rejected["error"] == "Incident id cannot contain path characters."
    assert idle.calls == []


@pytest.mark.asyncio
async def test_script_and_playbook_inserts_zip_text(monkeypatch):
    idle = _install(monkeypatch, uploads_module, RecordingFetcher())
    empty = _loaded(await insert_script(None, "  "))
    assert empty["success"] == "false"
    assert "empty" in empty["error"]
    assert idle.calls == []

    script = _install(monkeypatch, uploads_module, RecordingFetcher({"reply": {"success": True}}))
    uploaded = _loaded(await insert_script(None, "name: demo\n"))
    assert uploaded["success"] == "true"
    path, kwargs = script.calls[0]
    assert path == "/public_api/v1/scripts/insert"
    assert kwargs["omit_papi_prefix"] is True
    name, payload, mime = kwargs["files"]["file"]
    assert name == "script.yml.zip"
    assert mime == "application/zip"
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        assert archive.read("script.yml") == b"name: demo\n"

    playbook = _install(monkeypatch, uploads_module, RecordingFetcher({"reply": {"success": True}}))
    assert _loaded(await insert_playbook(None, "name: play\n"))["success"] == "true"
    path, kwargs = playbook.calls[0]
    assert path == "/public_api/v1/playbooks/insert"
    name, payload, mime = kwargs["files"]["file"]
    assert name == "playbook.yml.zip"
    assert mime == "application/zip"
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        assert archive.read("playbook.yml") == b"name: play\n"

    _install(monkeypatch, uploads_module, RecordingFetcher(PAPIAuthenticationError("denied")))
    error = _loaded(await insert_script(None, "name: demo\n"))
    assert error["success"] == "false"
    assert "denied" in error["error"]


@pytest.mark.asyncio
async def test_xql_stream_decodes_gzip_and_rejects_an_empty_id(monkeypatch):
    idle = _install(monkeypatch, xql_module, RecordingFetcher())
    empty = _loaded(await post_xql_get_query_results_stream(None, " "))
    assert empty["success"] == "false"
    assert "empty" in empty["error"]
    assert idle.calls == []

    raw = gzip.compress(b'{"reply":{"status":"SUCCESS"}}')
    fetcher = _install(monkeypatch, xql_module, RecordingFetcher(raw))
    payload = _loaded(await post_xql_get_query_results_stream(None, "stream-1"))
    assert payload["reply"]["status"] == "SUCCESS"
    path, kwargs = fetcher.calls[0]
    assert path == "/public_api/v1/xql/get_query_results_stream"
    assert kwargs["raw"] is True
    assert kwargs["omit_papi_prefix"] is True
    # Issue #44: the download gets the whole call budget, not an unreachable 300 s.
    assert kwargs["timeout"] == get_config().cortex_call_deadline_s
    assert kwargs["data"]["request_data"] == {"stream_id": "stream-1", "is_gzip_compressed": True}

    _install(monkeypatch, xql_module, RecordingFetcher({"reply": "not-bytes"}))
    error = _loaded(await post_xql_get_query_results_stream(None, "stream-1"))
    assert error["success"] == "false"
    assert "byte payload" in error["error"]


@pytest.mark.asyncio
async def test_run_xql_query_covers_failure_pending_and_missing_id(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(xql_module.asyncio, "sleep", no_sleep)

    missing = _install(monkeypatch, xql_module, RecordingFetcher({"reply": {}}))
    error = _loaded(await run_xql_query(None, "dataset=xdr_data | limit 1"))
    assert error["success"] == "false"
    assert "execution_id" in error["error"]
    assert len(missing.calls) == 1

    failed = _install(
        monkeypatch,
        xql_module,
        RecordingFetcher({"reply": "exec-2"}, {"reply": {"status": "FAILED", "error": "bad query"}}),
    )
    error = _loaded(await run_xql_query(None, "dataset=xdr_data | limit 1"))
    assert error["success"] == "false"
    assert "bad query" in error["error"]
    assert len(failed.calls) == 2

    pending = _install(
        monkeypatch,
        xql_module,
        RecordingFetcher(
            {"reply": {"execution_id": "exec-3"}},
            {"reply": {"status": "PENDING"}},
            {"reply": {"status": "SUCCESS", "results": [{"row": 1}]}},
        ),
    )
    payload = _loaded(await run_xql_query(None, "dataset=xdr_data | limit 1"))
    assert payload["reply"]["status"] == "SUCCESS"
    assert pending.calls[1][1]["data"]["request_data"]["query_id"] == "exec-3"
    assert len(pending.calls) == 3

    monkeypatch.setattr(xql_module, "_MAX_POLL_ATTEMPTS", 1)
    timed_out = _install(
        monkeypatch,
        xql_module,
        RecordingFetcher({"reply": "exec-4"}, {"reply": {"status": "PENDING"}}),
    )
    error = _loaded(await run_xql_query(None, "dataset=xdr_data | limit 1"))
    assert error["success"] == "false"
    assert "timed out" in error["error"]
    assert len(timed_out.calls) == 2

    _install(
        monkeypatch,
        xql_module,
        RecordingFetcher({"reply": "exec-5"}, {"reply": {"status": "UNKNOWN"}}),
    )
    error = _loaded(await run_xql_query(None, "dataset=xdr_data | limit 1"))
    assert error["success"] == "false"
    assert "UNKNOWN" in error["error"]

    papi_error = _install(monkeypatch, xql_module, RecordingFetcher(PAPIConnectionError("offline")))
    error = _loaded(await run_xql_query(None, "dataset=xdr_data | limit 1"))
    assert error["success"] == "false"
    assert "offline" in error["error"]
    assert len(papi_error.calls) == 1


@pytest.mark.asyncio
async def test_xsoar_yaml_task_and_credentials_post_their_bodies(monkeypatch):
    idle = _install(monkeypatch, xsoar_module, RecordingFetcher())
    empty_yaml = _loaded(await post_playbook_save_yaml(None, " "))
    assert empty_yaml["success"] == "false"
    assert idle.calls == []

    yaml_fetcher = _install(monkeypatch, xsoar_module, RecordingFetcher({"reply": "saved"}))
    saved = _loaded(await post_playbook_save_yaml(None, "name: play\n", file_name="play.yml"))
    assert saved["reply"] == "saved"
    path, kwargs = yaml_fetcher.calls[0]
    assert path == "/xsoar/public/v1/playbook/save/yaml"
    assert kwargs["omit_papi_prefix"] is True
    assert kwargs["files"]["file"][0] == "play.yml"
    assert kwargs["files"]["file"][1] == b"name: play\n"
    assert kwargs["files"]["file"][2] == "application/yaml"

    idle_task = _install(monkeypatch, xsoar_module, RecordingFetcher())
    empty_task = _loaded(await post_inv_playbook_task_complete(None, " ", "comment", "task", "input"))
    assert empty_task["success"] == "false"
    assert empty_task["error"] == "Investigation ID is empty."
    assert idle_task.calls == []

    task = _install(monkeypatch, xsoar_module, RecordingFetcher({"reply": "done"}))
    completed = _loaded(await post_inv_playbook_task_complete(None, "inv-1", "note", "task-1", "yes"))
    assert completed["success"] == "true"
    files = task.calls[0][1]["files"]
    assert task.calls[0][0] == "/xsoar/public/v1/inv-playbook/task/complete"
    assert task.calls[0][1]["omit_papi_prefix"] is True
    assert files["investigationId"] == (None, "inv-1")
    assert files["fileComment"] == (None, "note")
    assert files["taskId"] == (None, "task-1")
    assert files["taskInput"] == (None, "yes")

    empty_credentials = _loaded(await put_settings_credentials(None, {}))
    assert empty_credentials["success"] == "false"
    assert "non-empty" in empty_credentials["error"]

    credentials = _install(monkeypatch, xsoar_module, RecordingFetcher({"reply": "stored"}))
    stored = _loaded(await put_settings_credentials(None, {"name": "api", "user": "analyst"}))
    assert stored["reply"] == "stored"
    path, kwargs = credentials.calls[0]
    assert path == "/xsoar/public/v1/settings/credentials"
    assert kwargs["method"] == "PUT"
    assert kwargs["data"] == {"name": "api", "user": "analyst"}
    assert kwargs["omit_papi_prefix"] is True

    _install(monkeypatch, xsoar_module, RecordingFetcher(PAPIAuthenticationError("denied")))
    error = _loaded(await put_settings_credentials(None, {"name": "api"}))
    assert error["success"] == "false"
    assert "denied" in error["error"]
