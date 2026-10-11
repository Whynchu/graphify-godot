"""Godot text-resource extraction and cross-file link coverage."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from graphify.analyze import _LANG_FAMILY
from graphify.build import build_from_json
from graphify.detect import CODE_EXTENSIONS
from graphify.extract import _DISPATCH, collect_files, extract
from graphify.extractors import LANGUAGE_EXTRACTORS
from graphify.extractors.godot_text import extract_godot_text


_needs_gdscript = pytest.mark.skipif(
    importlib.util.find_spec("tree_sitter_gdscript") is None,
    reason="tree-sitter-gdscript not installed (optional [godot] extra)",
)


def _write_project(root: Path) -> None:
    (root / "project.godot").write_text(
        "[application]\nconfig/name=\"Fixture\"\n",
        encoding="utf-8",
    )


def test_godot_text_files_are_discovered_as_code(tmp_path: Path):
    _write_project(tmp_path)
    resource = tmp_path / "demo.tres"
    scene = tmp_path / "demo.tscn"
    resource.write_text("[gd_resource type=\"Resource\" format=3]\n[resource]\n", encoding="utf-8")
    scene.write_text("[gd_scene format=3]\n", encoding="utf-8")

    assert {".tres", ".tscn"} <= CODE_EXTENSIONS
    assert _DISPATCH[".tres"] is extract_godot_text
    assert _DISPATCH[".tscn"] is extract_godot_text
    assert LANGUAGE_EXTRACTORS["godot_text"] is extract_godot_text
    assert _LANG_FAMILY[".gd"] == _LANG_FAMILY[".tres"] == _LANG_FAMILY[".tscn"] == "gdscript"
    assert resource in collect_files(tmp_path)
    assert scene in collect_files(tmp_path)


def test_resource_parser_keeps_nested_links_and_never_scans_strings(tmp_path: Path):
    _write_project(tmp_path)
    (tmp_path / "thing.gd").write_text("class_name Thing\nextends Resource\n", encoding="utf-8")
    resource = tmp_path / "thing.tres"
    resource.write_text(
        '[gd_resource type="Resource" script_class="Thing" load_steps=3 format=3]\n'
        '[ext_resource type="Script" path="res://thing.gd" id="script"]\n'
        '[sub_resource type="Resource" id="nested"]\n'
        'resource_name = "Nested"\n'
        'label = "ExtResource(\\"fake\\")"\n'
        '[resource]\n'
        'script = ExtResource("script")\n'
        'id = &"healer"\n'
        'entries = Array[Resource]([\n'
        '    SubResource("nested"),\n'
        '])\n'
        'numbers = PackedInt32Array(1, 2, 3)\n'
        'vector = Vector2(1, 2)\n',
        encoding="utf-8",
    )

    result = extract_godot_text(resource, root=tmp_path)
    resource_node = next(
        node for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "resource"
    )
    nested_node = next(
        node for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "sub_resource"
    )
    metadata = resource_node["metadata"]

    assert metadata["godot_properties"]["id"] == {"type": "StringName", "value": "healer"}
    assert metadata["godot_properties"]["entries"] == {"type": "Array", "size": 1}
    assert metadata["godot_properties"]["numbers"] == {"type": "PackedInt32Array", "size": 3}
    assert metadata["godot_properties"]["vector"] == {"type": "Vector2"}
    assert {ref["target_kind"] for ref in metadata["godot_references"]} == {
        "ext_resource", "sub_resource",
    }
    nested_refs = nested_node["metadata"]["godot_references"]
    assert nested_refs == []
    assert not any(ref.get("target_id") == "fake" for ref in metadata["godot_references"])
    assert result["godot_facts"]["project_id"] == "."
    assert result["godot_facts"]["res_path"] == "res://thing.tres"
    assert not result["diagnostics"]

    graph_result = extract(
        [tmp_path / "thing.gd", resource],
        root=tmp_path,
        parallel=False,
    )
    script_class = next(
        node for node in graph_result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    resource_node = next(
        node for node in graph_result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "resource"
    )
    nested_node = next(
        node for node in graph_result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "sub_resource"
    )
    attachment = next(edge for edge in graph_result["edges"] if edge.get("context") == "godot_script_attachment")
    subresource_link = next(
        edge for edge in graph_result["edges"]
        if edge.get("context") == "godot_resource_property"
        and edge.get("target") == nested_node["id"]
    )
    assert attachment["source"] == resource_node["id"]
    assert attachment["target"] == script_class["id"]
    assert subresource_link["source"] == resource_node["id"]


def test_cyclic_tres_references_resolve_without_recursion(tmp_path: Path):
    _write_project(tmp_path)
    first = tmp_path / "first.tres"
    second = tmp_path / "second.tres"
    first.write_text(
        '[gd_resource type="Resource" format=3]\n'
        '[ext_resource type="Resource" path="res://second.tres" id="other"]\n'
        '[resource]\npeer = ExtResource("other")\n',
        encoding="utf-8",
    )
    second.write_text(
        '[gd_resource type="Resource" format=3]\n'
        '[ext_resource type="Resource" path="res://first.tres" id="other"]\n'
        '[resource]\npeer = ExtResource("other")\n',
        encoding="utf-8",
    )

    result = extract([first, second], root=tmp_path, parallel=False)
    links = [edge for edge in result["edges"] if edge.get("context") == "godot_resource_property"]
    assert len(links) == 2
    assert all(edge["source"] != edge["target"] for edge in links)
    assert not any(
        (node.get("metadata") or {}).get("godot_text", {}).get("diagnostics")
        for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "file"
    )


def test_nested_projects_keep_identical_res_paths_separate(tmp_path: Path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root, label in ((first_root, "First"), (second_root, "Second")):
        root.mkdir()
        _write_project(root)
        (root / "shared.tres").write_text(
            '[gd_resource type="Resource" format=3]\n'
            '[resource]\nresource_name = "' + label + '"\n',
            encoding="utf-8",
        )
        (root / "main.tscn").write_text(
            '[gd_scene format=3]\n'
            '[ext_resource type="Resource" path="res://shared.tres" id="shared"]\n'
            '[node name="Root" type="Node2D"]\n'
            'resource = ExtResource("shared")\n',
            encoding="utf-8",
        )

    paths = [
        first_root / "shared.tres", first_root / "main.tscn",
        second_root / "shared.tres", second_root / "main.tscn",
    ]
    result = extract(paths, root=tmp_path, parallel=False)
    resources_by_project = {
        (node.get("metadata") or {}).get("godot_project_id"): node
        for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "resource"
    }
    scene_nodes = {
        (node.get("metadata") or {}).get("godot_project_id"): node
        for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "scene_node"
        and node.get("label") == "Root"
    }
    links = [edge for edge in result["edges"] if edge.get("context") == "godot_resource_property"]

    assert set(resources_by_project) == {"first", "second"}
    assert {
        edge["target"]
        for edge in links
        if edge["source"] == scene_nodes["first"]["id"]
    } == {resources_by_project["first"]["id"]}
    assert {
        edge["target"]
        for edge in links
        if edge["source"] == scene_nodes["second"]["id"]
    } == {resources_by_project["second"]["id"]}


def test_unindexed_asset_reference_is_reported_without_a_phantom_node(tmp_path: Path):
    _write_project(tmp_path)
    resource = tmp_path / "icon.tres"
    resource.write_text(
        '[gd_resource type="Resource" format=3]\n'
        '[ext_resource type="Texture2D" path="res://art/icon.png" id="icon"]\n'
        '[resource]\ntexture = ExtResource("icon")\n',
        encoding="utf-8",
    )

    result = extract([resource], root=tmp_path, parallel=False)
    file_node = next(
        node for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "file"
    )
    diagnostics = file_node["metadata"]["godot_text"]["diagnostics"]

    assert any("outside the indexed graph" in item["message"] for item in diagnostics)
    assert not any(node.get("label") == "icon.png" for node in result["nodes"])


@_needs_gdscript
def test_scene_links_script_uid_instance_and_connected_method(tmp_path: Path):
    _write_project(tmp_path)
    script = tmp_path / "handler.gd"
    script.write_text(
        "class_name Handler\n"
        "func run_callback() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    script.with_name(script.name + ".uid").write_text("uid://handler\n", encoding="utf-8")
    child_scene = tmp_path / "child.tscn"
    child_scene.write_text(
        '[gd_scene format=3]\n'
        '[node name="ChildRoot" type="Node2D"]\n',
        encoding="utf-8",
    )
    scene = tmp_path / "main.tscn"
    scene.write_text(
        '[gd_scene load_steps=3 format=3 uid="uid://main"]\n'
        '[ext_resource type="Script" path="res://handler.gd" uid="uid://handler" id="script"]\n'
        '[ext_resource type="PackedScene" path="res://child.tscn" id="child"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("script")\n'
        '[node name="Button" type="Button" parent="."]\n'
        '[node name="Locator" type="Node" parent="."]\n'
        'target_path = NodePath("../Button")\n'
        '[node name="Child" parent="." instance=ExtResource("child")]\n'
        '[connection signal="pressed" from="Button" to="." method="run_callback"]\n',
        encoding="utf-8",
    )

    result = extract([script, child_scene, scene], root=tmp_path, parallel=False)
    nodes = {node["id"]: node for node in result["nodes"]}
    script_class = next(
        node for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    method = next(node for node in result["nodes"] if node.get("label") == "run_callback()")
    references = result["edges"]

    attachment = next(edge for edge in references if edge.get("context") == "godot_script_attachment")
    assert attachment["target"] == script_class["id"]
    assert attachment["relation"] == "references"

    instance = next(edge for edge in references if edge.get("context") == "godot_scene_instance")
    assert instance["relation"] == "uses"
    assert nodes[instance["target"]]["label"] == "ChildRoot"

    callback = next(edge for edge in references if edge.get("context") == "godot_signal_connection")
    assert callback["target"] == method["id"]
    assert callback["metadata"]["godot_callback_resolved"] is True
    assert callback["confidence"] == "EXTRACTED"

    node_path = next(edge for edge in references if edge.get("context") == "godot_node_path")
    assert nodes[node_path["source"]]["label"] == "Locator"
    assert nodes[node_path["target"]]["label"] == "Button"
    assert node_path["metadata"]["godot_target_node_path"] == "Button"

    graph = build_from_json(result, directed=True, root=tmp_path)
    assert graph.has_edge(attachment["source"], attachment["target"])
    assert graph.has_edge(instance["source"], instance["target"])
    assert graph.has_edge(callback["source"], method["id"])
    assert all(edge["source"] in nodes and edge["target"] in nodes for edge in references)

    # Re-resolve one changed scene against the persisted nodes/edges for the
    # unchanged scripts and child scene; cached Godot metadata carries its facts.
    unchanged_nodes = [
        node for node in result["nodes"]
        if Path(str(node.get("source_file", ""))).name != scene.name
    ]
    unchanged_edges = [
        edge for edge in result["edges"]
        if Path(str(edge.get("source_file", ""))).name != scene.name
    ]
    incremental = extract(
        [scene],
        root=tmp_path,
        parallel=False,
        resolution_context_nodes=unchanged_nodes,
        resolution_context_edges=unchanged_edges,
    )
    incremental_contexts = {edge.get("context") for edge in incremental["edges"]}
    assert {
        "godot_script_attachment", "godot_scene_instance",
        "godot_node_path", "godot_signal_connection",
    } <= incremental_contexts


def test_unresolved_scene_nodepaths_report_without_phantom_edges(tmp_path: Path):
    _write_project(tmp_path)
    scene = tmp_path / "paths.tscn"
    scene.write_text(
        '[gd_scene format=3]\n'
        '[node name="Root" type="Node2D"]\n'
        'self_path = NodePath(".")\n'
        '[node name="Locator" type="Node" parent="."]\n'
        'missing_path = NodePath("../Missing")\n'
        'absolute_path = NodePath("/root/Viewport")\n'
        'escape_path = NodePath("../../Outside")\n',
        encoding="utf-8",
    )

    result = extract([scene], root=tmp_path, parallel=False)
    node_path_edges = [edge for edge in result["edges"] if edge.get("context") == "godot_node_path"]
    file_node = next(
        node for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "file"
    )
    diagnostics = file_node["metadata"]["godot_text"]["diagnostics"]

    assert node_path_edges == []
    assert sum("NodePath" in item["message"] for item in diagnostics) == 3
