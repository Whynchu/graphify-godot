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
from graphify.extractors.godot_text import extract_godot_project, extract_godot_text


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
    assert ".godot" in CODE_EXTENSIONS
    assert _DISPATCH[".tres"] is extract_godot_text
    assert _DISPATCH[".tscn"] is extract_godot_text
    assert _DISPATCH[".godot"] is extract_godot_project
    assert LANGUAGE_EXTRACTORS["godot_text"] is extract_godot_text
    assert _LANG_FAMILY[".gd"] == _LANG_FAMILY[".tres"] == _LANG_FAMILY[".tscn"] == "gdscript"
    assert resource in collect_files(tmp_path)
    assert scene in collect_files(tmp_path)


@_needs_gdscript
def test_project_settings_resolve_main_scene_and_autoload_calls(tmp_path: Path):
    project = tmp_path / "project.godot"
    project.write_text(
        '[application]\n'
        'config/name="Project Fixture"\n'
        'run/main_scene="res://main.tscn"\n'
        '[autoload]\n'
        'GameState="*res://game_state.gd"\n',
        encoding="utf-8",
    )
    scene = tmp_path / "main.tscn"
    scene.write_text('[gd_scene format=3]\n[node name="Main" type="Node"]\n', encoding="utf-8")
    autoload = tmp_path / "game_state.gd"
    autoload.write_text("func reset() -> void:\n    pass\n", encoding="utf-8")
    caller = tmp_path / "caller.gd"
    caller.write_text("func tick() -> void:\n    GameState.reset()\n", encoding="utf-8")

    project_only = extract_godot_project(project, root=tmp_path)
    project_facts = project_only["godot_facts"]
    assert project_facts["main_scene"] == "res://main.tscn"
    assert project_facts["autoloads"] == [{
        "name": "GameState", "path": "res://game_state.gd", "singleton": True,
        "line": 5, "key": "autoload:GameState",
    }]

    result = extract([project, scene, autoload, caller], root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    contexts = {
        (labels[edge["source"]], labels[edge["target"]], edge.get("context"))
        for edge in result["edges"]
    }
    assert ("application/run/main_scene", "Main", "godot_project_main_scene") in contexts
    assert ("GameState", "game_state.gd", "godot_project_autoload") not in contexts
    assert ("GameState", "game_state", "godot_project_autoload") in contexts
    assert ("tick", "reset", "godot_gdscript_call") in contexts


def test_project_config_header_and_semicolon_comments_are_valid(tmp_path: Path):
    project = tmp_path / "project.godot"
    project.write_text(
        "; Godot project comment\n"
        "config_version=5\n"
        "[application]\n"
        'config/name="Fixture; Name" ; trailing comment\n'
        'run/main_scene="res://main.tscn"\n',
        encoding="utf-8",
    )

    result = extract_godot_project(project, root=tmp_path)
    facts = result["godot_facts"]
    assert facts["config_version"] == 5
    assert facts["main_scene"] == "res://main.tscn"
    assert not [item for item in facts["diagnostics"] if item["kind"] == "godot_text_parse_error"]
    project_node = next(node for node in result["nodes"] if node["label"] == "Fixture; Name")
    assert project_node["metadata"]["godot_kind"] == "godot_project"

    malformed = tmp_path / "malformed_project.godot"
    malformed.write_text(
        "unexpected_header=value\n"
        "[application\n",
        encoding="utf-8",
    )
    malformed_facts = extract_godot_project(malformed, root=tmp_path)["godot_facts"]
    assert any(item["kind"] == "godot_text_parse_error" for item in malformed_facts["diagnostics"])


@_needs_gdscript
def test_gdscript_node_paths_unique_names_and_attached_receiver_calls(tmp_path: Path):
    _write_project(tmp_path)
    enemy = tmp_path / "enemy_actor.gd"
    enemy.write_text("class_name EnemyActor\nfunc take_damage() -> void:\n    pass\n", encoding="utf-8")
    caller = tmp_path / "scene_caller.gd"
    caller.write_text(
        "class_name SceneCaller\n"
        "func attack() -> void:\n"
        "    $\"../Enemy\".take_damage()\n"
        "    var enemy_ref := get_node(\"../Enemy\")\n"
        "    enemy_ref.take_damage()\n"
        "    var named_ref := %Target\n"
        "    named_ref.take_damage()\n",
        encoding="utf-8",
    )
    scene = tmp_path / "main.tscn"
    scene.write_text(
        '[gd_scene format=3]\n'
        '[ext_resource type="Script" path="res://enemy_actor.gd" id="enemy_script"]\n'
        '[ext_resource type="Script" path="res://scene_caller.gd" id="caller_script"]\n'
        '[node name="Main" type="Node"]\n'
        '[node name="Enemy" type="Node" parent="."]\n'
        'script = ExtResource("enemy_script")\n'
        '[node name="Target" type="Node" parent="."]\n'
        'unique_name_in_owner = true\n'
        'script = ExtResource("enemy_script")\n'
        '[node name="Caller" type="Node" parent="."]\n'
        'script = ExtResource("caller_script")\n',
        encoding="utf-8",
    )

    result = extract([enemy, caller, scene], root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    scene_links = [edge for edge in result["edges"] if edge.get("context") in {"godot_script_node_path", "godot_script_unique_node"}]
    attached_calls = [
        edge for edge in result["edges"]
        if edge.get("relation") == "calls"
        and edge.get("context") in {"godot_scene_attached_call", "godot_gdscript_call"}
        and labels[edge["target"]] == "take_damage"
    ]

    assert {labels[edge["target"]] for edge in scene_links} >= {"Enemy", "Target"}
    assert any(edge.get("context") == "godot_script_unique_node" and labels[edge["target"]] == "Target" for edge in scene_links)
    assert len(attached_calls) == 3
    assert all(labels[edge["source"]] == "attack" and labels[edge["target"]] == "take_damage" for edge in attached_calls)


@_needs_gdscript
def test_inherited_scene_nodes_and_attached_scripts_resolve_for_node_paths(tmp_path: Path):
    _write_project(tmp_path)
    child_script = tmp_path / "child_actor.gd"
    child_script.write_text(
        "class_name ChildActor\n"
        "func ping() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    caller_script = tmp_path / "derived_actor.gd"
    caller_script.write_text(
        "class_name DerivedActor\n"
        "func use_child() -> void:\n"
        "    var child := $Child\n"
        "    child.ping()\n",
        encoding="utf-8",
    )
    base_scene = tmp_path / "base.tscn"
    base_scene.write_text(
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://child_actor.gd" id="child_script"]\n'
        '[node name="Base" type="Node"]\n'
        '[node name="Child" type="Node" parent="."]\n'
        'script = ExtResource("child_script")\n',
        encoding="utf-8",
    )
    derived_scene = tmp_path / "derived.tscn"
    derived_scene.write_text(
        '[gd_scene load_steps=3 format=3]\n'
        '[ext_resource type="PackedScene" path="res://base.tscn" id="base_scene"]\n'
        '[ext_resource type="Script" path="res://derived_actor.gd" id="derived_script"]\n'
        '[node name="Base" instance=ExtResource("base_scene")]\n'
        'script = ExtResource("derived_script")\n',
        encoding="utf-8",
    )

    result = extract([child_script, caller_script, base_scene, derived_scene], root=tmp_path, parallel=False)
    labels = {node["id"]: str(node.get("label") or "").strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    node_paths = [edge for edge in result["edges"] if edge.get("context") == "godot_script_node_path"]
    attached_calls = [
        edge for edge in result["edges"]
        if edge.get("relation") == "calls" and edge.get("context") == "godot_scene_attached_call"
    ]

    assert any(labels[edge["target"]] == "Child" for edge in node_paths)
    assert any(labels[edge["source"]] == "use_child" and labels[edge["target"]] == "ping" for edge in attached_calls)


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


@_needs_gdscript
def test_tres_assignments_and_property_accessors_link_to_script_definitions(tmp_path: Path):
    _write_project(tmp_path)
    script = tmp_path / "enemy_definition.gd"
    script.write_text(
        "class_name EnemyDefinition\n"
        "extends Resource\n"
        "@export var health: int = 0: set = set_health, get = get_health\n"
        "@export var label: String = \"\":\n"
        "    set(value):\n"
        "        label = value\n"
        "    get:\n"
        "        return label\n"
        "func set_health(value: int) -> void:\n"
        "    health = value\n"
        "func get_health() -> int:\n"
        "    return health\n",
        encoding="utf-8",
    )
    resource = tmp_path / "enemy.tres"
    resource.write_text(
        '[gd_resource type="Resource" script_class="EnemyDefinition" load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://enemy_definition.gd" id="script"]\n'
        '[resource]\n'
        'script = ExtResource("script")\n'
        'health = 12\n'
        'label = "Bat"\n',
        encoding="utf-8",
    )

    result = extract([script, resource], root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    property_nodes = {
        node["label"]: node
        for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "script_property"
    }
    health_metadata = property_nodes["health"]["metadata"]
    label_metadata = property_nodes["label"]["metadata"]
    assert (health_metadata["godot_setter"], health_metadata["godot_getter"]) == ("set_health", "get_health")
    assert health_metadata["godot_accessor_style"] == "named"
    assert label_metadata["godot_setter"] == "<inline>"
    assert label_metadata["godot_getter"] == "<inline>"
    assert label_metadata["godot_accessor_style"] == "inline"

    edges = result["edges"]
    rows = {(labels[edge["source"]], labels[edge["target"]], edge.get("context")) for edge in edges}
    assert ("health", "set_health", "godot_script_property_setter") in rows
    assert ("health", "get_health", "godot_script_property_getter") in rows
    assignments = [edge for edge in edges if edge.get("context") == "godot_serialized_property_assignment"]
    assert {(edge["source_location"], labels[edge["target"]]) for edge in assignments} == {
        ("L5", "health"), ("L6", "label"),
    }
    assert any(edge.get("context") == "godot_script_property_read" and edge["target"] == property_nodes["health"]["id"] for edge in edges)


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


@_needs_gdscript
def test_gdscript_smoke_dependencies_resolve_to_resources_and_global_classes(tmp_path: Path):
    _write_project(tmp_path)
    scripts = tmp_path / "scripts"
    data = tmp_path / "data"
    scenes = tmp_path / "scenes"
    tests = tmp_path / "tests"
    for directory in (scripts, data, scenes, tests):
        directory.mkdir()

    base = scripts / "base_controller.gd"
    base.write_text("class_name BaseController\nextends RefCounted\n", encoding="utf-8")
    target = scripts / "target_controller.gd"
    target.write_text(
        "class_name TargetController\nextends BaseController\n"
        "static func create() -> TargetController:\n"
        "    return TargetController.new()\n",
        encoding="utf-8",
    )
    resource = data / "profile.tres"
    resource.write_text('[gd_resource type="Resource" format=3]\n[resource]\n', encoding="utf-8")
    scene = scenes / "widget.tscn"
    scene.write_text('[gd_scene format=3]\n[node name="Widget" type="Node2D"]\n', encoding="utf-8")
    smoke = tests / "target_controller_smoke.gd"
    smoke.write_text(
        "extends SceneTree\n"
        'const TARGET = preload("res://scripts/target_controller.gd")\n'
        'const PROFILE = preload("res://data/profile.tres")\n'
        'const WIDGET = load("res://scenes/widget.tscn")\n'
        'const MISSING = preload("res://scripts/missing_controller.gd")\n'
        'const TEXT_ONLY = "TargetController and res://scripts/missing_controller.gd"\n'
        "func run() -> void:\n"
        "    var target: TargetController = TargetController.new()\n"
        "    target.create()\n"
        "    TARGET.create()\n",
        encoding="utf-8",
    )

    result = extract([base, target, resource, scene, smoke], root=tmp_path, parallel=False)
    nodes = {node["id"]: node for node in result["nodes"]}
    target_class = next(
        node for node in result["nodes"]
        if node["label"] == "TargetController"
        and (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    smoke_class = next(
        node for node in result["nodes"]
        if node["label"] == "target_controller_smoke"
        and (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    uses = [edge for edge in result["edges"] if edge.get("source") == smoke_class["id"]]
    assert any(
        edge.get("context") == "godot_script_preload" and edge.get("target") == target_class["id"]
        for edge in uses
    )
    assert any(
        edge.get("context") == "godot_script_preload"
        and nodes[edge["target"]].get("source_file") == "data/profile.tres"
        for edge in uses
    )
    assert any(
        edge.get("context") == "godot_script_load"
        and nodes[edge["target"]].get("label") == "Widget"
        for edge in uses
    )
    smoke_methods = {
        node["id"]: node for node in result["nodes"]
        if node.get("source_file") == "tests/target_controller_smoke.gd"
        and str(node.get("label") or "").strip().lstrip(".").removesuffix("()") == "run"
    }
    target_method = next(node for node in result["nodes"] if node.get("label") == "create()")
    assert any(
        edge.get("source") in smoke_methods and edge.get("target") == target_method["id"]
        for edge in result["edges"]
    )
    assert any(
        edge.get("context") == "godot_global_class_reference"
        and edge.get("target") == target_class["id"]
        for edge in uses
    )
    assert not any(
        edge.get("source") == smoke_class["id"]
        and "missing_controller.gd" in str((edge.get("metadata") or {}).get("godot_resource_path", ""))
        for edge in result["edges"]
    )
    smoke_file = next(node for node in result["nodes"] if node.get("source_file") == "tests/target_controller_smoke.gd" and node.get("label") == "target_controller_smoke.gd")
    unresolved = smoke_file["metadata"]["godot_script"]["resolution_diagnostics"]
    assert any("missing_controller.gd" in item["message"] for item in unresolved)

    unchanged_nodes = [node for node in result["nodes"] if node.get("source_file") != smoke_file["source_file"]]
    unchanged_edges = [edge for edge in result["edges"] if edge.get("source_file") != smoke_file["source_file"]]
    incremental = extract(
        [smoke],
        root=tmp_path,
        parallel=False,
        resolution_context_nodes=unchanged_nodes,
        resolution_context_edges=unchanged_edges,
    )
    assert any(
        edge.get("context") == "godot_script_preload" and edge.get("target") == target_class["id"]
        for edge in incremental["edges"]
    )
    assert any(
        edge.get("source") in smoke_methods and edge.get("target") == target_method["id"]
        for edge in incremental["edges"]
    )

    def source_edge_facts(graph):
        return {
            (
                edge.get("relation"), edge.get("context"), edge.get("source"),
                edge.get("target"), edge.get("source_location"), edge.get("confidence"),
            )
            for edge in graph["edges"]
            if edge.get("source_file") == smoke_file["source_file"]
        }

    assert source_edge_facts(result) == source_edge_facts(incremental)


@_needs_gdscript
def test_renamed_gdscript_target_does_not_retain_old_preload_or_call_edges(tmp_path: Path):
    _write_project(tmp_path)
    original = tmp_path / "target.gd"
    original.write_text(
        "class_name RenameTarget\n"
        "static func ping() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.gd"
    caller.write_text(
        'const TARGET = preload("res://target.gd")\n'
        "func run() -> void:\n"
        "    TARGET.ping()\n",
        encoding="utf-8",
    )
    cache_root = tmp_path / "graphify-cache"
    first = extract([original, caller], root=tmp_path, cache_root=cache_root, parallel=False)
    assert any(edge.get("context") == "godot_script_preload" for edge in first["edges"])
    assert any(edge.get("context") == "godot_gdscript_call" for edge in first["edges"])

    renamed = tmp_path / "renamed_target.gd"
    original.rename(renamed)
    second = extract([renamed, caller], root=tmp_path, cache_root=cache_root, parallel=False)
    assert not any(edge.get("context") in {"godot_script_preload", "godot_gdscript_call"} for edge in second["edges"])
    caller_file = next(node for node in second["nodes"] if node.get("label") == "caller.gd" and node.get("source_file") == "caller.gd")
    diagnostics = caller_file["metadata"]["godot_script"]["resolution_diagnostics"]
    assert any("target.gd" in item["message"] for item in diagnostics)


@_needs_gdscript
def test_dynamic_godot_boundaries_report_reasons_without_guessed_edges(tmp_path: Path):
    _write_project(tmp_path)
    target = tmp_path / "target.gd"
    target.write_text(
        "class_name BoundaryTarget\n"
        "func remote_method() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.gd"
    caller.write_text(
        "class_name BoundaryCaller\n"
        "signal changed\n"
        "func local_method() -> void:\n"
        "    pass\n"
        "func run(receiver, method_name, resource_path) -> void:\n"
        "    self.call(\"local_method\")\n"
        "    receiver.call(method_name)\n"
        "    receiver.call_deferred(\"remote_method\")\n"
        "    get_tree().call_group(\"enemies\", \"remote_method\")\n"
        "    get_tree().get_nodes_in_group(\"enemies\")\n"
        "    receiver.set_script(load(\"res://target.gd\"))\n"
        "    add_child(Node.new())\n"
        "    changed.connect(Callable(receiver, method_name))\n"
        "    load(resource_path)\n",
        encoding="utf-8",
    )

    result = extract([target, caller], root=tmp_path, parallel=False)
    caller_class = next(
        node for node in result["nodes"]
        if node.get("label") == "BoundaryCaller"
        and (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    target_class = next(
        node for node in result["nodes"]
        if node.get("label") == "BoundaryTarget"
        and (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    labels = {node["id"]: str(node.get("label") or "").strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    calls = [edge for edge in result["edges"] if edge.get("relation") == "calls"]
    assert any(edge.get("context") == "godot_dynamic_literal_call" and labels[edge["target"]] == "local_method" for edge in calls)
    assert not any(edge.get("source") == caller_class["id"] and edge.get("target") == target_class["id"] for edge in calls)

    caller_file = next(
        node for node in result["nodes"]
        if node.get("label") == "caller.gd" and node.get("source_file") == "caller.gd"
    )
    messages = [item["message"] for item in caller_file["metadata"]["godot_script"]["resolution_diagnostics"]]
    assert any("Computed load path" in message for message in messages)
    assert any("Dynamic call remains unresolved" in message for message in messages)
    assert any("Dynamic call_deferred remains unresolved" in message for message in messages)
    assert any("runtime group membership" in message for message in messages)
    assert any("runtime group lookup" in message for message in messages)
    assert any("Runtime set_script()" in message for message in messages)
    assert any("Runtime add_child()" in message for message in messages)
    assert any("callback remains dynamic" in message for message in messages)


@_needs_gdscript
def test_duplicate_global_class_names_do_not_create_guessed_edges(tmp_path: Path):
    _write_project(tmp_path)
    scripts = tmp_path / "scripts"
    tests = tmp_path / "tests"
    scripts.mkdir()
    tests.mkdir()
    (scripts / "first.gd").write_text("class_name SharedController\nextends RefCounted\n", encoding="utf-8")
    (scripts / "second.gd").write_text("class_name SharedController\nextends RefCounted\n", encoding="utf-8")
    smoke = tests / "duplicate_name_smoke.gd"
    smoke.write_text(
        "extends SceneTree\n"
        "func run() -> void:\n"
        "    var controller: SharedController\n",
        encoding="utf-8",
    )

    result = extract([scripts / "first.gd", scripts / "second.gd", smoke], root=tmp_path, parallel=False)
    assert not any(
        edge.get("context") == "godot_global_class_reference"
        for edge in result["edges"]
    )
    smoke_node = next(
        node for node in result["nodes"]
        if node.get("source_file") == "tests/duplicate_name_smoke.gd" and node.get("label") == "duplicate_name_smoke.gd"
    )
    diagnostics = smoke_node["metadata"]["godot_script"]["resolution_diagnostics"]
    assert any("matches multiple scripts" in item["message"] for item in diagnostics)
