"""GDScript discovery and structural extraction coverage."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from graphify.build import build_from_json
from graphify.detect import CODE_EXTENSIONS
from graphify.extract import _DISPATCH, extract_gdscript
from graphify.extractors import LANGUAGE_EXTRACTORS


_needs_gdscript = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_gdscript") is None,
    reason="tree-sitter-gdscript not installed (optional [godot] extra)",
)


def test_gdscript_is_discovered_and_dispatched():
    from graphify.analyze import _LANG_FAMILY
    from graphify.build import _EDGE_LANG_FAMILY
    from graphify.extract import _EXTRA_FOR_EXTENSION

    assert ".gd" in CODE_EXTENSIONS
    assert _DISPATCH[".gd"] is extract_gdscript
    assert LANGUAGE_EXTRACTORS["gdscript"] is extract_gdscript
    assert _EXTRA_FOR_EXTENSION[".gd"] == "godot"
    assert _LANG_FAMILY[".gd"] == "gdscript"
    assert _EDGE_LANG_FAMILY[".gd"] == "gdscript"


@_needs_gdscript
def test_script_class_methods_and_exported_properties_are_extracted(tmp_path: Path):
    path = tmp_path / "demo_definition.gd"
    path.write_text(
        "@tool\n"
        "extends Resource\n"
        "class_name DemoDefinition\n"
        "@export var id: StringName = &\"\"\n"
        "var display_name := \"\"\n"
        "func validate() -> bool:\n"
        "    return not id.is_empty()\n",
        encoding="utf-8",
    )

    result = extract_gdscript(path)

    assert "error" not in result
    class_node = next(
        node for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    assert class_node["label"] == "DemoDefinition"
    assert class_node["metadata"]["godot_global_class"] is True
    assert class_node["metadata"]["godot_base_type"] == "Resource"

    exported_property = next(
        node for node in result["nodes"]
        if node["label"] == "id"
        and (node.get("metadata") or {}).get("godot_kind") == "script_property"
    )
    assert exported_property["metadata"]["godot_exported"] is True
    assert exported_property["metadata"]["godot_declared_type"] == "StringName"

    method = next(node for node in result["nodes"] if node["label"] == "validate()")
    assert any(
        edge["relation"] == "method"
        and edge["source"] == class_node["id"]
        and edge["target"] == method["id"]
        for edge in result["edges"]
    )

    graph = build_from_json(result, directed=True, root=tmp_path)
    graph_class_id = next(
        node_id for node_id, attrs in graph.nodes(data=True)
        if attrs["label"] == "DemoDefinition"
        and (attrs.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    graph_property_id = next(
        node_id for node_id, attrs in graph.nodes(data=True)
        if attrs["label"] == "id"
        and (attrs.get("metadata") or {}).get("godot_kind") == "script_property"
    )
    graph_method_id = next(
        node_id for node_id, attrs in graph.nodes(data=True)
        if attrs["label"] == "validate()"
    )
    assert graph.nodes[graph_class_id]["metadata"]["godot_base_type"] == "Resource"
    assert graph.nodes[graph_property_id]["metadata"]["godot_exported"] is True
    assert graph.get_edge_data(graph_class_id, graph_method_id)["relation"] == "method"


@_needs_gdscript
def test_parser_recovery_is_retained_as_a_diagnostic(tmp_path: Path):
    path = tmp_path / "partial.gd"
    path.write_text(
        "class_name PartialScript\n"
        "func kept() -> void:\n"
        "    pass\n"
        "func broken(:\n"
        "    pass\n",
        encoding="utf-8",
    )

    result = extract_gdscript(path)

    assert result["parse_errors"]["first_error_line"] == 4
    file_node = next(node for node in result["nodes"] if node["label"] == "partial.gd")
    diagnostic = file_node["metadata"]["godot_script"]["parser_diagnostics"][0]
    assert diagnostic["kind"] == "godot_parse_error"
    assert diagnostic["line"] == 4
