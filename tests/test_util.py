import pytest

from pkg import util
from pkg.util import get_papi_url, path_segment_error, read_file


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tenant.example", "https://api-tenant.example"),
        ("http://tenant.example", "https://api-tenant.example"),
        ("https://tenant.example", "https://api-tenant.example"),
        ("https://api-tenant.example", "https://api-tenant.example"),
        ("https://api-tenant.example/", "https://api-tenant.example"),
        ("tenant.example:8443", "https://api-tenant.example:8443"),
        ("http://tenant.example:8443//", "https://api-tenant.example:8443"),
        ("https://my-api-tenant.example", "https://api-my-api-tenant.example"),
        ("https://tenant.example/api-gateway/", "https://api-tenant.example/api-gateway"),
    ],
)
def test_get_papi_url_forces_https_and_prefixes_the_hostname(raw, expected):
    assert get_papi_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "https://", "https://:8443"])
def test_get_papi_url_rejects_a_missing_hostname(raw):
    with pytest.raises(ValueError):
        get_papi_url(raw)


def test_read_file_rejects_a_sibling_directory_that_shares_the_prefix(tmp_path):
    resources = tmp_path / "res"
    resources.mkdir()
    (resources / "ok.json").write_text("{}")
    sibling = tmp_path / "res_evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret")

    assert read_file("ok.json", resources) == "{}"
    with pytest.raises(ValueError, match="path traversal"):
        read_file("../res_evil/secret.txt", resources)


def test_bundle_openapi_files_rejects_a_template_outside_the_openapi_dir(tmp_path, monkeypatch):
    openapi_dir = tmp_path / "openapi"
    openapi_dir.mkdir()
    sibling = tmp_path / "openapi_evil"
    sibling.mkdir()
    (sibling / "openapi.yaml").write_text("openapi: 3.0.3\n")
    (openapi_dir / "openapi.yaml").symlink_to(sibling / "openapi.yaml")
    monkeypatch.setattr(util, "OPENAPI_DIR", openapi_dir)
    monkeypatch.setattr(util, "bundle_specs", lambda *args: pytest.fail("bundled a template outside OPENAPI_DIR"))

    with pytest.raises(ValueError, match="path traversal"):
        util.bundle_openapi_files()


@pytest.mark.parametrize("value", ["1/../../x", "1?x", "1#x", "1%2F", "a\\b", "..", ".", " 1"])
def test_path_segment_error_rejects_values_that_leave_the_segment(value):
    assert path_segment_error(value, "Incident id") == "Incident id cannot contain path characters."


def test_path_segment_error_accepts_a_single_segment():
    assert path_segment_error("42", "Incident id") is None
    assert path_segment_error("INC-1..2", "Incident id") is None
    assert path_segment_error(" ", "Incident id") == "Incident id is empty."
