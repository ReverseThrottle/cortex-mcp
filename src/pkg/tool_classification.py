"""Explicit side-effect classes for catalog and handwritten tools.

Annotations, registration, and write confirmation all use this classification.
Description text is not a classification. A missing class is not read-only.
"""

from collections.abc import Iterator
from typing import Any, Literal, assert_never

from config.config import get_config
from pkg.util import bundle_openapi_from_folders

SideEffect = Literal["read", "action", "write", "secret"]

SIDE_EFFECT_EXTENSION = "x-cortex-mcp-side-effect"
ADDITIVE_EXTENSION = "x-cortex-mcp-destructive"

_HTTP_METHODS: frozenset[str] = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

# Handwritten tools are not OpenAPI operations. The class is recorded here so a
# docstring that says "Side effects: none" cannot mark a mutating tool read-only.
HANDWRITTEN_SIDE_EFFECTS: dict[str, SideEffect] = {
    "get_cases": "read",
    "get_issues": "read",
    "get_performance_incident_export_by_incident_id": "read",
    "post_playbooks_get": "read",
    "post_scripts_get": "read",
    "post_xql_get_query_results_stream": "read",
    "run_xql_query": "write",
    "update_case": "write",
    "isolate_endpoint": "action",
    "unisolate_endpoint": "action",
    "insert_script": "write",
    "insert_playbook": "write",
    "post_playbook_save_yaml": "write",
    "post_entry_upload_by_incident_id": "write",
    "post_incident_upload_by_incident_id": "write",
    "post_inv_playbook_task_complete": "write",
    "put_settings_credentials": "write",
    "post_auth_reset_initial_password": "write",
    "post_auth_token": "write",
    "post_logs": "write",
    "post_network_interface": "write",
    "post_network_internal_subnet": "write",
    "post_network_proxy": "write",
    "post_network_ntp": "write",
    "post_network_ssl_certificate": "write",
    "post_network_trusted_ca": "write",
    "post_register": "write",
}

# Handwritten write/action tools that only add data. Every other tool that
# writes is destructive: absence is the safe direction. Members must be
# handwritten write or action tools (tests/test_tool_annotations.py).
HANDWRITTEN_ADDITIVE: frozenset[str] = frozenset()

_catalog_cache: dict[str, SideEffect] | None = None
_catalog_additive_cache: frozenset[str] | None = None


def parse_side_effect(value: object) -> SideEffect | None:
    """Return a known class, or None when the value is missing or unrecognized."""
    match value:
        case "read":
            return "read"
        case "action":
            return "action"
        case "write":
            return "write"
        case "secret":
            return "secret"
        case _:
            return None


def handwritten_side_effect(name: str) -> SideEffect | None:
    return HANDWRITTEN_SIDE_EFFECTS.get(name)


def side_effect_sentence(kind: SideEffect | None, method: str | None = None, path: str | None = None) -> str:
    """Sentence published on a tool so the description matches the classification."""
    call = f" ({method.upper()} {path})" if method and path else ""
    match kind:
        case "read":
            return f"Side effects: none. This is a read-only Cortex API call{call}."
        case "action":
            return (
                f"Side effects: this operation starts a Cortex action{call}. "
                "It is not a read-only call. Confirm the target before calling."
            )
        case "secret":
            return (
                f"Side effects: this operation returns secret or credential material{call}. "
                "It is not a read-only call. Confirm the target before calling."
            )
        case "write" | None:
            return (
                f"Side effects: this operation changes Cortex tenant state{call}. "
                "It can create, update, or delete data. Confirm the target before calling."
            )
        case _:
            assert_never(kind)


def description_matches_side_effect(description: str | None, kind: SideEffect | None) -> bool:
    """True when the leading side-effect sentence agrees with the classification."""
    first = ""
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    match kind:
        case "read":
            return first.startswith("Side effects: none")
        case "action":
            # Handwritten actions already say the call changes tenant state.
            return first.startswith("Side effects: this operation starts a Cortex action") or first.startswith(
                "Side effects: this operation changes"
            )
        case "secret":
            return first.startswith("Side effects: this operation returns secret or credential material")
        case "write":
            return first.startswith("Side effects: this operation changes") or first.startswith(
                "Side effects: this operation creates"
            )
        case None:
            return first.startswith(
                (
                    "Side effects: this operation changes",
                    "Side effects: this operation creates",
                    "Side effects: this operation starts",
                    "Side effects: this operation returns secret",
                )
            )
        case _:
            assert_never(kind)


def align_description(
    description: str | None,
    kind: SideEffect | None,
    *,
    method: str | None = None,
    path: str | None = None,
) -> str:
    """Replace a side-effect sentence that disagrees with the classification.

    A read sentence on an action, write, secret, or unclassified tool is rewritten.
    Descriptions that already agree are left unchanged, including their formatting.
    """
    text = description or ""
    if description_matches_side_effect(text, kind):
        return text
    sentence = side_effect_sentence(kind, method, path)
    stripped = text.strip()
    if not stripped:
        return sentence
    if stripped.startswith("Side effects:"):
        rest = ""
        if "\n\n" in text:
            rest = text.split("\n\n", 1)[1]
        elif "\n" in text:
            rest = text.split("\n", 1)[1]
        rest = rest.strip()
        if rest:
            return f"{sentence}\n\n{rest}"
        return sentence
    return f"{sentence}\n\n{stripped}"


def catalog_tool_enabled(kind: SideEffect | None, name: str | None = None) -> bool:
    """Read tools are always registered. Other classes follow the feature flags.

    ``MCP_TOOLS_DENY`` removes a named tool whatever its class. ``MCP_TOOLS_ALLOW``
    registers a named write or action tool without ``MCP_WRITE_TOOLS_ENABLED``.
    It never lifts the secret or isolate flags. Secret tools need both write
    tools and secret tools. Unclassified tools need write tools and are never
    lifted by the allow list, because an unclassified tool is not known to be safe.
    """
    config = get_config()
    if name is not None and name in config.tools_deny_set:
        return False
    match kind:
        case "read":
            return True
        case "secret":
            return config.write_tools_enabled and config.secret_tools_enabled
        case "action" | "write":
            return config.write_tools_enabled or (name is not None and name in config.tools_allow_set)
        case None:
            return config.write_tools_enabled
        case _:
            assert_never(kind)


def tool_denied(name: str) -> bool:
    return name in get_config().tools_deny_set


def write_tool_enabled(name: str) -> bool:
    """Handwritten write or action tool: the write flag or the allow list, and not denied."""
    config = get_config()
    if name in config.tools_deny_set:
        return False
    return config.write_tools_enabled or name in config.tools_allow_set


def requires_confirmation(kind: SideEffect | None) -> bool:
    """Action, write, secret, and unclassified tools ask before they run."""
    return kind != "read"


def _iter_catalog_operations() -> Iterator[tuple[str, dict[str, Any]]]:
    spec = bundle_openapi_from_folders()
    for methods in spec.get("paths", {}).values():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str):
                yield operation_id, operation


def catalog_operation_ids() -> frozenset[str]:
    """Every catalog operationId, classified or not. Not cached: startup calls it once."""
    return frozenset(operation_id for operation_id, _ in _iter_catalog_operations())


def _catalog_classifications() -> dict[str, SideEffect]:
    global _catalog_cache
    if _catalog_cache is None:
        found: dict[str, SideEffect] = {}
        for operation_id, operation in _iter_catalog_operations():
            kind = parse_side_effect(operation.get(SIDE_EFFECT_EXTENSION))
            if kind is not None:
                found[operation_id] = kind
        _catalog_cache = found
    return _catalog_cache


def catalog_classifications() -> dict[str, SideEffect]:
    """Return a copy of the catalog operation-id to side-effect map."""
    return dict(_catalog_classifications())


def _catalog_additive() -> frozenset[str]:
    global _catalog_additive_cache
    if _catalog_additive_cache is None:
        classes = _catalog_classifications()
        _catalog_additive_cache = frozenset(
            operation_id
            for operation_id, operation in _iter_catalog_operations()
            if operation.get(ADDITIVE_EXTENSION) is False and classes.get(operation_id) in ("write", "action")
        )
    return _catalog_additive_cache


def additive_from_route(route: Any) -> bool:
    """True only for a write or action operation explicitly marked additive."""
    if side_effect_from_route(route) not in ("write", "action"):
        return False
    extensions = getattr(route, "extensions", None)
    if isinstance(extensions, dict) and ADDITIVE_EXTENSION in extensions:
        return extensions[ADDITIVE_EXTENSION] is False
    operation_id = getattr(route, "operation_id", None)
    if isinstance(operation_id, str) and operation_id:
        return operation_id in _catalog_additive()
    return False


def side_effect_from_route(route: Any) -> SideEffect | None:
    """Read the operation class from route extensions, then from the bundled spec."""
    extensions = getattr(route, "extensions", None)
    if isinstance(extensions, dict) and SIDE_EFFECT_EXTENSION in extensions:
        return parse_side_effect(extensions.get(SIDE_EFFECT_EXTENSION))
    operation_id = getattr(route, "operation_id", None)
    if isinstance(operation_id, str) and operation_id:
        return _catalog_classifications().get(operation_id)
    return None


def remember_side_effect(component: Any, kind: SideEffect | None) -> None:
    """Keep the class on the tool object. Annotations remain the client-visible copy."""
    object.__setattr__(component, "_cortex_side_effect", kind)
    fields = getattr(type(component), "model_fields", {})
    if "meta" not in fields or kind is None:
        return
    current = getattr(component, "meta", None)
    merged = dict(current or {})
    merged[SIDE_EFFECT_EXTENSION] = kind
    try:
        component.meta = merged
    except Exception:
        return


def side_effect_from_tool(tool: Any) -> SideEffect | None:
    """Return the class stored on a tool. Description text is ignored.

    A tool with no stored class and no read-only annotation is unclassified.
    """
    if isinstance(tool, str) or tool is None:
        return None
    if hasattr(tool, "_cortex_side_effect"):
        return parse_side_effect(tool._cortex_side_effect)
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict) and SIDE_EFFECT_EXTENSION in meta:
        return parse_side_effect(meta.get(SIDE_EFFECT_EXTENSION))
    direct = getattr(tool, "side_effect", None)
    parsed = parse_side_effect(direct)
    if parsed is not None:
        return parsed
    annotations = getattr(tool, "annotations", None)
    read_only = getattr(annotations, "read_only_hint", None) if annotations is not None else None
    if read_only is True:
        return "read"
    if read_only is False:
        return "write"
    return None
