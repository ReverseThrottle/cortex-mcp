from pathlib import Path

import yaml


def test_vulnerabilities_next_page_token_is_required():
    spec = yaml.safe_load(Path("src/usecase/builtin_components/openapi/get_vulnerabilities.yaml").read_text())
    operation = spec["paths"]["/public_api/uvem/v1/get_vulnerabilities"]["post"]
    payload = spec["components"]["schemas"]["VulnerabilitiesRequestPayload"]
    assert "next_page_token" in payload["required"]
    assert 'empty string ""' in payload["properties"]["next_page_token"]["description"]
    assert 'empty string ""' in operation["description"]
    body = operation["requestBody"]["description"]
    assert "next_page_token" in body
    assert 'empty string ""' in body
    assert "An empty request_data object returns all results" not in body
    examples = operation["requestBody"]["content"]["application/json"]["examples"]
    assert examples
    for example in examples.values():
        assert example["value"]["request_data"]["next_page_token"] == ""
