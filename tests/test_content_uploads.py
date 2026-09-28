import zipfile
from io import BytesIO

import httpx
import pytest

from pkg.client import PAPIClient
from usecase.builtin_components.content_uploads import (
    _validate_upload_text,
    zip_named_text,
)


def test_zip_named_text_round_trip():
    payload = zip_named_text("script.yml", "name: demo\n")
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        assert archive.read("script.yml") == b"name: demo\n"


def test_upload_text_rejects_empty_nul_and_oversized():
    assert _validate_upload_text("   ", "Script") == "Script content is empty."
    assert _validate_upload_text("a\x00b", "Script") == "Script content must be text, not a binary payload."
    assert _validate_upload_text("x" * 1_000_001, "Playbook") is not None
    assert _validate_upload_text("name: ok\n", "Script") is None


@pytest.mark.asyncio
async def test_multipart_request_keeps_tenant_auth_and_drops_json_content_type():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content-type"] = request.headers.get("content-type", "")
        captured["authorization"] = request.headers.get("authorization")
        captured["auth-id"] = request.headers.get("x-xdr-auth-id")
        captured["body"] = request.content
        return httpx.Response(200, json={"reply": {"success": True}})

    client = PAPIClient(
        "https://api.example.invalid",
        {"Authorization": "tenant-secret", "x-xdr-auth-id": "42"},
        transport=httpx.MockTransport(handler),
    )
    result = await client.request(
        "POST",
        "/public_api/v1/scripts/insert",
        files={"file": ("script.yml.zip", b"PK\x03\x04", "application/zip")},
    )
    await client.aclose()

    assert result == {"reply": {"success": True}}
    assert captured["authorization"] == "tenant-secret"
    assert captured["auth-id"] == "42"
    assert "application/json" not in captured["content-type"]
    assert captured["content-type"].startswith("multipart/form-data")
    assert b"script.yml.zip" in captured["body"]
