from pathlib import Path

import yaml


def test_vulnerabilities_next_page_token_is_required():
    spec = yaml.safe_load(
        Path("src/usecase/builtin_components/openapi/get_vulnerabilities.yaml").read_text()
    )
    payload = spec["components"]["schemas"]["VulnerabilitiesRequestPayload"]
    assert "next_page_token" in payload["required"]
    assert 'empty string ""' in payload["properties"]["next_page_token"]["description"]
