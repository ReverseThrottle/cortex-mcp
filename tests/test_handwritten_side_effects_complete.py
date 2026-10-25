"""Handwritten SideEffect registry must stay complete and helpers must agree."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastmcp import FastMCP

from pkg.tool_classification import (
    HANDWRITTEN_SIDE_EFFECTS,
    align_description,
    description_matches_side_effect,
    parse_side_effect,
)
from usecase.base_module import BaseModule

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def _handwritten_tool_names() -> set[str]:
    """AST-discover production `_add_tool` targets plus `_BROKER_TOOLS` entries."""
    names: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_add_tool":
                if not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Name):
                    # Skip the broker loop variable; those names come from _BROKER_TOOLS.
                    if arg.id == "tool":
                        continue
                    names.add(arg.id)
                elif isinstance(arg, ast.Attribute):
                    names.add(arg.attr)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "_BROKER_TOOLS"
                        and isinstance(node.value, (ast.Tuple, ast.List))
                    ):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Name):
                                names.add(elt.id)
    return names


def test_handwritten_side_effects_cover_every_registered_tool():
    registered = _handwritten_tool_names()
    declared = set(HANDWRITTEN_SIDE_EFFECTS)
    assert registered == declared, (
        f"missing from HANDWRITTEN_SIDE_EFFECTS: {sorted(registered - declared)}; "
        f"extra in HANDWRITTEN_SIDE_EFFECTS: {sorted(declared - registered)}"
    )


def test_parse_side_effect_accepts_known_classes_only():
    assert parse_side_effect("read") == "read"
    assert parse_side_effect("action") == "action"
    assert parse_side_effect("write") == "write"
    assert parse_side_effect("secret") == "secret"
    assert parse_side_effect("unknown") is None
    assert parse_side_effect(None) is None
    assert parse_side_effect(1) is None


@pytest.mark.parametrize(
    ("kind", "description", "matches"),
    [
        ("read", "Side effects: none. This is a read-only Cortex API call.", True),
        ("read", "Side effects: this operation changes Cortex tenant state.", False),
        ("action", "Side effects: this operation starts a Cortex action.", True),
        ("action", "Side effects: this operation changes Cortex tenant state.", True),
        ("write", "Side effects: this operation changes Cortex tenant state.", True),
        ("write", "Side effects: this operation creates a resource.", True),
        ("secret", "Side effects: this operation returns secret or credential material.", True),
        ("secret", "Side effects: none.", False),
        (None, "Side effects: this operation changes Cortex tenant state.", True),
        (None, "Side effects: none.", False),
    ],
)
def test_description_matches_side_effect(kind, description, matches):
    assert description_matches_side_effect(description, kind) is matches


def test_align_description_rewrites_disagreeing_lead_sentence():
    original = "Side effects: none.\n\nDoes something useful."
    aligned = align_description(original, "write")
    assert aligned.startswith("Side effects: this operation changes")
    assert "Does something useful." in aligned


def test_align_description_leaves_matching_text_unchanged():
    original = "Side effects: none. This is a read-only Cortex API call.\n\nBody stays."
    assert align_description(original, "read") == original


def test_add_tool_raises_when_side_effect_missing():
    class _Probe(BaseModule):
        def register_tools(self):
            def mystery_tool() -> str:
                return "{}"

            self._add_tool(mystery_tool)

        def register_resources(self):
            return None

    mcp = FastMCP("probe")
    with pytest.raises(ValueError, match="mystery_tool"):
        _Probe(mcp).register_tools()
