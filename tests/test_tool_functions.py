import gzip
import json
import zipfile
from io import BytesIO

import pytest

from entities.exceptions import PAPIAuthenticationError, PAPIConnectionError
from usecase.builtin_components import cases as cases_module
from usecase.builtin_components import content_uploads as uploads_module
from usecase.builtin_components import issues as issues_module
from usecase.builtin_components import xsoar_actions as xsoar_module
from usecase.builtin_components.cases import get_cases
from usecase.builtin_components.content_uploads import insert_playbook, insert_script
from usecase.builtin_components.issues import get_issues
from usecase.builtin_components.xsoar_actions import (
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
async def test_get_issues_sends_the_search_path_and_metadata(monkeypatch):
    fetcher = _install(monkeypatch, issues_module, RecordingFetcher({"reply": [{"id": 3}]}))
    payload = _loaded(
        await get_issues(None, [{"field": "id", "operator": "in", "value": ["3"]}], search_from=0, search_to=10)
    )
    assert payload["success"] == "true"
    assert payload["reply"] == [{"id": 3}]
    assert "formatting_instructions" in payload["_metadata"]
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
    invalid = _loaded(await update_case(None, [1], status="nope"))
    assert invalid["success"] == "false"
    assert "Invalid status" in invalid["error"]
    assert idle.calls == []

    missing = _loaded(await update_case(None, [1]))
    assert missing["success"] == "false"
    assert "At least one" in missing["error"]

    invalid_severity = _loaded(await update_case(None, [1], severity="urgent"))
    assert invalid_severity["success"] == "false"
    assert "Invalid severity" in invalid_severity["error"]
    assert idle.calls == []

    fetcher = _install(monkeypatch, case_actions_module, RecordingFetcher({"reply": {"updated": True}}))
    updated = _loaded(
        await update_case(
            None,
            [4, 5],
            comment="note",
            status="under_investigation",
            severity="high",
            assigned_user_mail="analyst@example.com",
        )
    )
    assert updated["success"] == "true"
    request_data = fetcher.calls[0][1]["data"]["request_data"]
    assert fetcher.calls[0][0] == "case/update/"
    assert request_data["case_id_list"] == [4, 5]
    assert request_data["update_data"] == {
        "comment": "note",
        "status": "under_investigation",
        "severity": "high",
        "assigned_user_mail": "analyst@example.com",
    }

    _install(monkeypatch, case_actions_module, RecordingFetcher(PAPIAuthenticationError("denied")))
    error = _loaded(await update_case(None, [4], comment="note"))
    assert error["success"] == "false"
    assert "denied" in error["error"]


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
    assert kwargs["timeout"] == 300
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
