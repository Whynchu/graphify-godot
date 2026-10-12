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


@_needs_gdscript
def test_literal_resource_and_global_class_references_are_extracted(tmp_path: Path):
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="Fixture"\n', encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    path = scripts / "smoke.gd"
    path.write_text(
        "extends BaseController\n"
        'const TARGET = preload("res://scripts/target.gd")\n'
        'const DATA = load("res://data/profile.tres")\n'
        'const SCENE = ResourceLoader.load("res://scenes/widget.tscn")\n'
        'const TEXT_ONLY = "preload(\\\"res://missing.gd\\\")"\n'
        "func run() -> void:\n"
        "    var target: TargetController = TargetController.new()\n"
        "    pass\n",
        encoding="utf-8",
    )

    result = extract_gdscript(path, root=tmp_path)
    file_node = next(node for node in result["nodes"] if node["label"] == "smoke.gd")
    facts = file_node["metadata"]["godot_script"]

    assert facts["resource_references"] == [
        {"kind": "preload", "path": "res://scripts/target.gd", "line": 2, "binding": "TARGET", "owner_class_line": 1},
        {"kind": "load", "path": "res://data/profile.tres", "line": 3, "binding": "DATA", "owner_class_line": 1},
        {"kind": "load", "path": "res://scenes/widget.tscn", "line": 4, "binding": "SCENE", "owner_class_line": 1},
    ]
    class_refs = {(item["name"], item["kind"]) for item in facts["class_references"]}
    assert ("BaseController", "extends") in class_refs
    assert ("TargetController", "type") in class_refs
    assert ("TargetController", "class_receiver") in class_refs


@_needs_gdscript
def test_signals_enums_constants_and_nested_members_are_owned(tmp_path: Path):
    path = tmp_path / "declarations.gd"
    path.write_text(
        "class_name DeclarationDemo\n"
        "signal changed(value: int)\n"
        "enum State { IDLE, ACTIVE = 3 }\n"
        "const LIMIT := 4\n"
        "class Nested:\n"
        "    var health: int\n"
        "    func act() -> void:\n"
        "        pass\n"
        "",
        encoding="utf-8",
    )

    result = extract_gdscript(path)
    by_kind = {
        (node.get("metadata") or {}).get("godot_kind"): node
        for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind")
    }
    assert by_kind["script_signal"]["label"] == "changed"
    assert by_kind["script_signal"]["metadata"]["godot_signature"] == "(value: int)"
    assert by_kind["script_enum"]["label"] == "State"
    assert by_kind["script_constant"]["label"] == "LIMIT"
    nested = next(node for node in result["nodes"] if node["label"] == "Nested")
    nested_property = next(
        node for node in result["nodes"]
        if node["label"] == "health"
        and (node.get("metadata") or {}).get("godot_kind") == "script_property"
    )
    nested_method = next(node for node in result["nodes"] if node["label"].lstrip(".") == "act()")
    assert nested.get("metadata", {}).get("godot_kind") == "script_inner_class"
    assert any(e["source"] == nested["id"] and e["target"] == nested_property["id"] for e in result["edges"])
    assert any(e["source"] == nested["id"] and e["target"] == nested_method["id"] for e in result["edges"])


@_needs_gdscript
def test_same_script_calls_resolve_with_scope_and_shadowing_guards(tmp_path: Path):
    path = tmp_path / "call_graph.gd"
    path.write_text(
        "class_name CallGraphDemo\n"
        "class Nested:\n"
        "    func nested_target() -> void:\n"
        "        pass\n"
        "    func nested_caller() -> void:\n"
        "        nested_target()\n"
        "func target() -> void:\n"
        "    pass\n"
        "static func make() -> void:\n"
        "    pass\n"
        "func caller() -> void:\n"
        "    self.target()\n"
        "    target()\n"
        "    make()\n"
        "func shadowed() -> void:\n"
        "    var target = Callable()\n"
        "    target()\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([path], cache_root=tmp_path / "graphify-cache", root=tmp_path, parallel=False)
    calls = [edge for edge in result["edges"] if edge.get("relation") == "calls"]
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    call_pairs = {(labels[edge["source"]], labels[edge["target"]]) for edge in calls}
    assert ("caller", "target") in call_pairs
    assert ("caller", "make") in call_pairs
    assert ("nested_caller", "nested_target") in call_pairs
    assert ("shadowed", "target") not in call_pairs


@_needs_gdscript
def test_typed_cross_script_calls_resolve_only_from_declared_receiver_types(tmp_path: Path):
    base = tmp_path / "base_worker.gd"
    base.write_text(
        "class_name BaseWorker\n"
        "func perform() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    target = tmp_path / "worker.gd"
    target.write_text(
        "extends BaseWorker\n"
        "class_name Worker\n"
        "func perform() -> void:\n"
        "    pass\n"
        "static func create() -> Worker:\n"
        "    return Worker.new()\n",
        encoding="utf-8",
    )
    caller = tmp_path / "caller.gd"
    caller.write_text(
        "extends BaseWorker\n"
        "class_name Caller\n"
        'const WorkerAlias = preload("res://worker.gd")\n'
        "func target_method() -> void:\n"
        "    pass\n"
        "func run() -> void:\n"
        "    var worker: Worker = Worker.new()\n"
        "    worker.perform()\n"
        '    worker.call("perform")\n'
        "    Worker.create()\n"
        "func through_alias() -> void:\n"
        "    WorkerAlias.create()\n"
        "    var worker := WorkerAlias.new()\n"
        "    worker.perform()\n"
        "func through_self_call() -> void:\n"
        '    self.call("target_method")\n'
        "func by_parameter(worker: Worker) -> void:\n"
        "    worker.perform()\n"
        "func by_collection(workers: Array[Worker]) -> void:\n"
        "    workers[0].perform()\n"
        "func inferred_local() -> void:\n"
        "    var worker := Worker.new()\n"
        "    worker.perform()\n"
        "func dynamic_call(worker) -> void:\n"
        "    worker.perform()\n"
        "func call_super() -> void:\n"
        "    super.perform()\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([base, target, caller], cache_root=tmp_path / "graphify-cache", root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    calls = [edge for edge in result["edges"] if edge.get("relation") == "calls"]
    call_pairs = {(labels[edge["source"]], labels[edge["target"]], edge.get("confidence")) for edge in calls}
    assert ("run", "perform", "EXTRACTED") in call_pairs
    assert ("run", "create", "EXTRACTED") in call_pairs
    assert ("by_parameter", "perform", "EXTRACTED") in call_pairs
    assert ("by_collection", "perform", "EXTRACTED") in call_pairs
    assert ("call_super", "perform", "EXTRACTED") in call_pairs
    assert ("inferred_local", "perform", "INFERRED") in call_pairs
    assert ("run", "perform", "EXTRACTED") in call_pairs
    assert ("through_alias", "create", "EXTRACTED") in call_pairs
    assert ("through_alias", "perform", "INFERRED") in call_pairs
    assert ("through_self_call", "target_method", "EXTRACTED") in call_pairs
    assert not any(caller_name == "dynamic_call" for caller_name, _target_name, _confidence in call_pairs)


@_needs_gdscript
def test_receiver_reassignment_updates_or_invalidates_inferred_type(tmp_path: Path):
    first = tmp_path / "review_first.gd"
    first.write_text(
        "class_name ReviewFirst\n"
        "extends RefCounted\n"
        "func ping() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    second = tmp_path / "review_second.gd"
    second.write_text(
        "class_name ReviewSecond\n"
        "extends RefCounted\n"
        "func ping() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    caller = tmp_path / "review_caller.gd"
    caller.write_text(
        "class_name ReviewCaller\n"
        "func sequential() -> void:\n"
        "    var receiver := ReviewFirst.new()\n"
        "    receiver.ping()\n"
        "    receiver = ReviewSecond.new()\n"
        "    receiver.ping()\n"
        "func conditional(condition: bool) -> void:\n"
        "    var receiver := ReviewFirst.new()\n"
        "    if condition:\n"
        "        receiver = ReviewSecond.new()\n"
        "    receiver.ping()\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([first, second, caller], root=tmp_path, parallel=False)
    nodes = {node["id"]: node for node in result["nodes"]}
    method_owner = {
        edge["target"]: nodes[edge["source"]]["label"]
        for edge in result["edges"] if edge.get("relation") == "method"
    }
    calls = [edge for edge in result["edges"] if edge.get("relation") == "calls"]
    sequential = next(node for node in result["nodes"] if node.get("label") == "sequential()")
    conditional = next(node for node in result["nodes"] if node.get("label") == "conditional()")
    sequential_targets = {
        (edge.get("source_location"), method_owner.get(edge["target"]))
        for edge in calls if edge.get("source") == sequential["id"]
    }
    assert ("L4", "ReviewFirst") in sequential_targets
    assert ("L6", "ReviewSecond") in sequential_targets
    assert not any(edge.get("source") == conditional["id"] for edge in calls)

    caller_file = next(node for node in result["nodes"] if node.get("label") == "review_caller.gd")
    diagnostics = caller_file["metadata"]["godot_script"]["resolution_diagnostics"]
    assert any(item.get("kind") == "godot_receiver_type_invalidated" and item.get("line") == 11 for item in diagnostics)


@_needs_gdscript
def test_known_receiver_without_project_method_reports_unresolved_target(tmp_path: Path):
    target = tmp_path / "review_first.gd"
    target.write_text(
        "class_name ReviewFirst\n"
        "extends RefCounted\n"
        "func present() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated.gd"
    unrelated.write_text(
        "class_name Unrelated\n"
        "extends RefCounted\n"
        "func graphify_missing_probe() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    caller = tmp_path / "review_caller.gd"
    caller.write_text(
        "func inspect(target: ReviewFirst) -> void:\n"
        "    target.graphify_missing_probe()\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([target, unrelated, caller], root=tmp_path, parallel=False)
    assert not any(
        edge.get("relation") == "calls"
        and edge.get("source_location") == "L2"
        for edge in result["edges"]
    )
    caller_file = next(node for node in result["nodes"] if node.get("label") == "review_caller.gd")
    diagnostics = caller_file["metadata"]["godot_script"]["resolution_diagnostics"]
    assert any(
        item.get("kind") == "godot_receiver_method_not_found"
        and "graphify_missing_probe" in item.get("message", "")
        for item in diagnostics
    )


@_needs_gdscript
def test_typed_cross_script_property_reads_and_writes_resolve_to_definitions(tmp_path: Path):
    definition = tmp_path / "enemy_definition.gd"
    definition.write_text(
        "class_name EnemyDefinition\n"
        "extends Resource\n"
        "@export var max_health: int = 10\n",
        encoding="utf-8",
    )
    consumer = tmp_path / "enemy_consumer.gd"
    consumer.write_text(
        "class_name EnemyConsumer\n"
        "func tune(definition: EnemyDefinition) -> void:\n"
        "    var old_value := definition.max_health\n"
        "    definition.max_health = old_value + 1\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([definition, consumer], root=tmp_path, parallel=False)
    nodes = {node["id"]: node for node in result["nodes"]}
    max_health = next(
        node for node in result["nodes"]
        if node.get("label") == "max_health"
        and (node.get("metadata") or {}).get("godot_kind") == "script_property"
    )
    usages = [
        edge for edge in result["edges"]
        if edge.get("target") == max_health["id"] and str(edge.get("context") or "").startswith("godot_script_property_")
    ]
    assert {edge.get("context") for edge in usages} == {
        "godot_script_property_read", "godot_script_property_write",
    }
    assert all(nodes[edge["source"]]["label"] == "tune()" for edge in usages)


@_needs_gdscript
def test_script_property_reads_and_writes_resolve_to_property_nodes(tmp_path: Path):
    path = tmp_path / "property_usage.gd"
    path.write_text(
        "class_name PropertyUsage\n"
        "var score: int = 0\n"
        "var unused: String = \"\"\n"
        "func award(points: int) -> void:\n"
        "    score += points\n"
        "func current_score() -> int:\n"
        "    return self.score\n",
        encoding="utf-8",
    )
    from graphify.extract import extract
    result = extract([path], cache_root=tmp_path / "graphify-cache", root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"] for node in result["nodes"]}
    property_nodes = {
        node["label"]: node["id"] for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "script_property"
    }
    accesses = [edge for edge in result["edges"] if edge.get("context", "").startswith("godot_script_property_")]
    access_rows = {(labels[edge["source"]], labels[edge["target"]], edge["context"]) for edge in accesses}
    assert ("award()", "score", "godot_script_property_read_write") in access_rows
    assert ("current_score()", "score", "godot_script_property_read") in access_rows
    assert property_nodes["unused"] not in {edge["target"] for edge in accesses}


@_needs_gdscript
def test_script_signal_emits_and_literal_callbacks_resolve(tmp_path: Path):
    path = tmp_path / "signal_usage.gd"
    path.write_text(
        "class_name SignalUsage\n"
        "signal changed(value: int)\n"
        "func _on_changed(value: int) -> void:\n"
        "    pass\n"
        "func run() -> void:\n"
        "    changed.emit(1)\n"
        "    self.changed.emit(2)\n"
        "    changed.connect(_on_changed)\n"
        "    changed.connect(Callable(self, \"_on_changed\"))\n",
        encoding="utf-8",
    )
    from graphify.extract import extract
    result = extract([path], cache_root=tmp_path / "graphify-cache", root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    facts = [edge for edge in result["edges"] if edge.get("context", "").startswith("godot_script_signal_")]
    rows = {(labels[edge["source"]], labels[edge["target"]], edge["context"]) for edge in facts}
    assert ("run", "changed", "godot_script_signal_emit") in rows
    assert ("run", "changed", "godot_script_signal_connection") in rows
    assert ("changed", "_on_changed", "godot_script_signal_callback") in rows


@_needs_gdscript
def test_semicolon_separated_statements_parse_without_changing_source_lines(tmp_path: Path):
    path = tmp_path / "semicolon_statements.gd"
    path.write_text(
        "class_name SemicolonStatements\n"
        "func first() -> void:\n"
        "    pass\n"
        "func second() -> void:\n"
        "    pass\n"
        "func caller() -> void:\n"
        "    first(); second()\n"
        "    print(\"left;right\")\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([path], cache_root=tmp_path / "graphify-cache", root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    calls = [edge for edge in result["edges"] if edge.get("relation") == "calls"]
    caller_calls = [edge for edge in calls if labels[edge["source"]] == "caller"]

    assert {(labels[edge["target"]], edge.get("source_location")) for edge in caller_calls} >= {
        ("first", "L7"), ("second", "L7"),
    }
    file_node = next(node for node in result["nodes"] if (node.get("metadata") or {}).get("godot_kind") == "file")
    script_metadata = file_node["metadata"]["godot_script"]
    assert script_metadata["parser_normalizations"]["semicolon_statement_splits"] == 1
    assert not script_metadata.get("parser_diagnostics")


@_needs_gdscript
def test_inline_compound_statements_parse_with_original_locations(tmp_path: Path):
    path = tmp_path / "inline_flow.gd"
    path.write_text(
        "class_name InlineFlow\n"
        "func active(value: bool) -> void: if value: mark_active(); record()\n"
        "func loop(points: Array[Vector2]) -> void:\n"
        "    for point in points: if point.x > 0.0: record()\n"
        "func choose(value: bool) -> void:\n"
        "    if value: record()\n"
        "    elif not value: record()\n"
        "    else: pass\n"
        "func quoted() -> String:\n"
        '    return "if fake: not_a_block();" # if also_not_a_block: pass\n',
        encoding="utf-8",
    )

    result = extract_gdscript(path)
    file_node = next(node for node in result["nodes"] if node["label"] == "inline_flow.gd")
    facts = file_node["metadata"]["godot_script"]
    assert "parser_diagnostics" not in facts
    assert facts["parser_normalizations"]["inline_compound_splits"] >= 6
    active = next(node for node in result["nodes"] if node.get("label") == "active()")
    assert active["source_location"] == "L2"
    assert any(node.get("label") == "choose()" for node in result["nodes"])
    assert any(node.get("label") == "quoted()" for node in result["nodes"])
    assert all(reference.get("line") == 2 for reference in facts["call_references"] if reference.get("caller_name") == "active")


@_needs_gdscript
def test_inherited_script_signals_resolve_without_name_only_matches(tmp_path: Path):
    base = tmp_path / "signal_base.gd"
    base.write_text(
        "class_name SignalBase\n"
        "signal updated(value: int)\n",
        encoding="utf-8",
    )
    child = tmp_path / "signal_child.gd"
    child.write_text(
        "extends SignalBase\n"
        "class_name SignalChild\n"
        "func _on_updated(value: int) -> void:\n"
        "    pass\n"
        "func emit_update() -> void:\n"
        "    self.updated.emit(1)\n"
        "func connect_update() -> void:\n"
        "    self.updated.connect(Callable(self, \"_on_updated\"))\n"
        "func unrelated() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "other_signal.gd"
    unrelated.write_text(
        "class_name OtherSignalOwner\n"
        "signal updated(value: int)\n",
        encoding="utf-8",
    )

    from graphify.extract import extract
    result = extract([base, child, unrelated], cache_root=tmp_path / "graphify-cache", root=tmp_path, parallel=False)
    labels = {node["id"]: node["label"].strip().lstrip(".").removesuffix("()") for node in result["nodes"]}
    child_calls = {node_id for node_id, label in labels.items() if label in {"emit_update", "connect_update"}}
    signal_nodes = {
        node["id"] for node in result["nodes"]
        if (node.get("metadata") or {}).get("godot_kind") == "script_signal"
        and node["label"] == "updated"
    }
    signal_edges = [
        edge for edge in result["edges"]
        if edge.get("context", "").startswith("godot_script_signal_")
    ]
    rows = {(labels[edge["source"]], labels[edge["target"]], edge["context"]) for edge in signal_edges}

    assert {"emit_update", "connect_update"} <= {labels[node_id] for node_id in child_calls}
    assert ("emit_update", "updated", "godot_script_signal_emit") in rows
    assert ("connect_update", "updated", "godot_script_signal_connection") in rows
    assert ("updated", "_on_updated", "godot_script_signal_callback") in rows
    assert len(signal_nodes) == 2
    assert not any(labels[edge["source"]] == "unrelated" for edge in signal_edges)
