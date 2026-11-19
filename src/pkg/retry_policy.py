"""ReadTimeout retry policy for PAPIClient (issue #46).

A read timeout means the request was sent and the tenant may already have
applied it. Resending a non-idempotent operation can isolate an endpoint twice,
run a script twice, or generate extra API keys. This module decides, from the
request method and path alone, whether a ReadTimeout may be retried.

Mechanism — reuse of the tool side-effect classification, not a second copy:

* Catalog routes: the bundled OpenAPI spec (the same source
  ``pkg.tool_classification`` reads) carries ``x-cortex-mcp-side-effect`` on
  every operation. The map below is keyed by (HTTP method, path) instead of
  operationId, because the transport layer only sees the outgoing request.
  Path parameters (``{policyId}`` etc.) match one concrete segment.
* Handwritten tools: ``_HANDWRITTEN_ROUTES`` maps their fixed request paths to
  tool names; the class itself is read from ``HANDWRITTEN_SIDE_EFFECTS`` so the
  registry stays the single source of truth. Broker VM tools are absent on
  purpose: they use ``pkg.broker_client.BrokerClient``, which does not retry.
* Client helper routes: ``_HELPER_ROUTES`` covers read-only paths that are not
  OpenAPI operations (the content-update zip download).

Fail-safe default: a route with no class is not read-only — the same doctrine
as ``pkg.tool_classification``. An unclassified POST/PUT/PATCH/DELETE is treated
as potentially mutating and its ReadTimeout is not retried. GET/HEAD/OPTIONS
are read methods and stay retryable.

Only ReadTimeout is gated here. ConnectError/ConnectTimeout (the request was
never sent) and explicit HTTP 429/503 rejections keep the normal retry policy
on every path, and slow report/search reads stay never-retried on ReadTimeout.
"""

from urllib.parse import urlsplit

from pkg.call_deadline import is_slow_report_or_search
from pkg.tool_classification import (
    HANDWRITTEN_SIDE_EFFECTS,
    SIDE_EFFECT_EXTENSION,
    SideEffect,
    parse_side_effect,
)
from pkg.util import bundle_openapi_from_folders

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Handwritten tools are not OpenAPI operations. Each entry names the tool that
# sends this request; the side-effect class comes from HANDWRITTEN_SIDE_EFFECTS.
# Paths are normalized: lowercase, no trailing slash. {incident_id} matches one
# concrete segment. Broker VM tools use BrokerClient (no retries) and are not
# listed here.
_HANDWRITTEN_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/public_api/v1/case/search"): "get_cases",
    ("POST", "/public_api/v1/issue/search"): "get_issues",
    ("GET", "/xsoar/public/v1/performance/incident/export/{incident_id}"): (
        "get_performance_incident_export_by_incident_id"
    ),
    ("POST", "/public_api/v1/playbooks/get"): "post_playbooks_get",
    ("POST", "/public_api/v1/scripts/get"): "post_scripts_get",
    ("POST", "/public_api/v1/xql/get_query_results_stream"): "post_xql_get_query_results_stream",
    ("POST", "/public_api/v1/xql/start_xql_query"): "run_xql_query",
    ("POST", "/public_api/v1/case/update"): "update_case",
    ("POST", "/public_api/v1/endpoints/isolate"): "isolate_endpoint",
    ("POST", "/public_api/v1/endpoints/unisolate"): "unisolate_endpoint",
    ("POST", "/public_api/v1/scripts/insert"): "insert_script",
    ("POST", "/public_api/v1/playbooks/insert"): "insert_playbook",
    ("POST", "/xsoar/public/v1/playbook/save/yaml"): "post_playbook_save_yaml",
    ("POST", "/xsoar/public/v1/entry/upload/{incident_id}"): "post_entry_upload_by_incident_id",
    ("POST", "/xsoar/public/v1/incident/upload/{incident_id}"): "post_incident_upload_by_incident_id",
    ("POST", "/xsoar/public/v1/inv-playbook/task/complete"): "post_inv_playbook_task_complete",
    ("PUT", "/xsoar/public/v1/settings/credentials"): "put_settings_credentials",
}

# Read-only client helper paths that are not OpenAPI operations. The content
# update download (src/cli.py, PAPIClient.stream) only fetches a zip.
_HELPER_ROUTES: dict[tuple[str, str], SideEffect] = {
    ("POST", "/public_api/v1/mcp/download"): "read",
}

# Appended to the timeout sentence when a mutating request is not retried, so
# the caller knows the operation may already have run.
SIDE_EFFECT_TIMEOUT_NOTE = (
    " The request was sent and may already have been applied; it was not retried."
    " Check the tenant state before sending it again."
)

_route_cache: dict[tuple[str, str], SideEffect] | None = None


def _normalize_path(url: str) -> str:
    return urlsplit(url).path.lower().rstrip("/")


def _route_map() -> dict[tuple[str, str], SideEffect]:
    global _route_cache
    if _route_cache is None:
        routes: dict[tuple[str, str], SideEffect] = {}

        def add(key: tuple[str, str], kind: SideEffect) -> None:
            existing = routes.get(key)
            if existing is not None and existing != kind:
                raise ValueError(f"conflicting side-effect classes for {key[0]} {key[1]}: {existing} vs {kind}")
            routes[key] = kind

        spec = bundle_openapi_from_folders()
        for path, methods in spec.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                kind = parse_side_effect(operation.get(SIDE_EFFECT_EXTENSION))
                if kind is not None:
                    add((method.upper(), _normalize_path(path)), kind)
        for (method, path), tool_name in _HANDWRITTEN_ROUTES.items():
            add((method, path), HANDWRITTEN_SIDE_EFFECTS[tool_name])
        for key, kind in _HELPER_ROUTES.items():
            add(key, kind)
        _route_cache = routes
    return _route_cache


def _matches_template(template: str, path: str) -> bool:
    template_segments = template.split("/")
    path_segments = path.split("/")
    if len(template_segments) != len(path_segments):
        return False
    for expected, actual in zip(template_segments, path_segments, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            if not actual:
                return False
        elif expected != actual:
            return False
    return True


def route_side_effect(method: str, url: str) -> SideEffect | None:
    """Class of the route this request targets, or None when it is unknown.

    Accepts a full URL or a bare path. Query strings, case, and a trailing
    slash do not affect the lookup. Concrete path parameters match the
    catalog's ``{parameter}`` templates.
    """
    key = (method.upper(), _normalize_path(url))
    routes = _route_map()
    kind = routes.get(key)
    if kind is not None:
        return kind
    for (route_method, template), route_kind in routes.items():
        if route_method == key[0] and "{" in template and _matches_template(template, key[1]):
            return route_kind
    return None


def may_have_side_effects(method: str, url: str) -> bool:
    """True when the request may have changed tenant state or returned secrets.

    Fail-safe: an unclassified route is not read-only. Only GET/HEAD/OPTIONS
    are assumed read-only without a classification.
    """
    kind = route_side_effect(method, url)
    if kind is None:
        return method.upper() not in _READ_METHODS
    return kind != "read"


def read_timeout_is_retryable(method: str, url: str) -> bool:
    """Whether a ReadTimeout on this request may be retried.

    Never for slow report/search reads (they stall until the client gives up)
    and never for routes that may have side effects (the tenant may already
    have applied the request). Known reads keep the normal retry policy.
    """
    if is_slow_report_or_search(url):
        return False
    return not may_have_side_effects(method, url)
