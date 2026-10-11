"""GDScript extractor using the optional tree-sitter-gdscript grammar."""
from __future__ import annotations

import importlib
import re
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes
from graphify.extractors.engine import _extract_generic
from graphify.extractors.godot_text import (
    _decode_string,
    godot_project_context,
    read_uid_sidecar,
)
from graphify.extractors.models import LanguageConfig


_GDSCRIPT_CONFIG = LanguageConfig(
    ts_module="tree_sitter_gdscript",
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    function_boundary_types=frozenset({"function_definition"}),
)

_MAX_SCRIPT_RESOURCE_REFS = 10_000
_MAX_SCRIPT_CLASS_REFS = 50_000
_GODOT_SOURCE_SUFFIXES = frozenset({".gd", ".tres", ".tscn"})


def _node_text(node, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _script_reference_facts(root, source: bytes) -> tuple[list[dict], list[dict]]:
    """Collect bounded, syntax-backed script dependencies and class references.

    Resource references are limited to literal ``preload``/``load`` calls whose
    targets are Godot source resources. Class references are collected only in
    type, constructor, inheritance, and attribute-receiver positions; the
    project resolver later keeps names that match a unique ``class_name``.
    """
    resources: list[dict] = []
    class_refs: list[dict] = []
    resource_seen: set[tuple[str, str, int, str]] = set()
    class_seen: set[tuple[str, str, int]] = set()

    def const_binding(node) -> str:
        parent = node.parent
        while parent is not None:
            if parent.type == "const_statement":
                return _node_text(parent.child_by_field_name("name"), source)[:160]
            if parent.type in {"function_definition", "class_definition"}:
                break
            parent = parent.parent
        return ""

    def add_resource(kind: str, call_node, argument_node) -> None:
        if argument_node is None or argument_node.type != "string":
            return
        raw = _node_text(argument_node, source)
        path = _decode_string(raw)
        if not path:
            return
        suffix = Path(path).suffix.lower()
        is_godot_uri = path.startswith(("res://", "uid://"))
        if not is_godot_uri and suffix not in _GODOT_SOURCE_SUFFIXES:
            return
        if path.startswith("res://") and suffix not in _GODOT_SOURCE_SUFFIXES:
            return
        line = call_node.start_point[0] + 1
        binding = const_binding(call_node)
        key = (kind, path, line, binding)
        if key in resource_seen or len(resources) >= _MAX_SCRIPT_RESOURCE_REFS:
            return
        resource_seen.add(key)
        resources.append({"kind": kind, "path": path[:500], "line": line, "binding": binding})

    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))

        if node.type == "call":
            named = node.named_children
            if len(named) >= 2 and named[0].type == "identifier":
                kind = _node_text(named[0], source)
                if kind in {"preload", "load"}:
                    args = next((child for child in named[1:] if child.type == "arguments"), None)
                    argument = args.named_children[0] if args and args.named_children else None
                    add_resource(kind, node, argument)

        elif node.type == "attribute_call" and node.parent is not None and node.parent.type == "attribute":
            # ResourceLoader.load() is a static load boundary equivalent to
            # load(), while avoiding guesses about arbitrary receiver methods.
            attribute = node.parent
            named = attribute.named_children
            call_parts = node.named_children
            if (
                len(named) >= 2
                and _node_text(named[0], source) == "ResourceLoader"
                and call_parts
                and _node_text(call_parts[0], source) == "load"
            ):
                args = next((child for child in call_parts[1:] if child.type == "arguments"), None)
                argument = args.named_children[0] if args and args.named_children else None
                add_resource("load", attribute, argument)

        if node.type != "identifier":
            continue
        name = _node_text(node, source)
        parent = node.parent
        if not name or parent is None:
            continue
        ref_kind = ""
        if parent.type == "type":
            ref_kind = "extends" if parent.parent is not None and parent.parent.type == "extends_statement" else "type"
        elif parent.type == "attribute" and parent.named_children and parent.named_children[0].id == node.id:
            ref_kind = "class_receiver"
        elif parent.type == "call" and parent.named_children and parent.named_children[0].id == node.id:
            ref_kind = "constructor"
        if not ref_kind:
            continue
        line = node.start_point[0] + 1
        key = (name, ref_kind, line)
        if key in class_seen or len(class_refs) >= _MAX_SCRIPT_CLASS_REFS:
            continue
        class_seen.add(key)
        class_refs.append({"name": name[:160], "kind": ref_kind, "line": line})

    resources.sort(key=lambda item: (item["line"], item["kind"], item["path"], item["binding"]))
    class_refs.sort(key=lambda item: (item["line"], item["name"], item["kind"]))
    return resources, class_refs


def _add_script_metadata(path: Path, source: bytes, result: dict, scan_root: Path | None = None) -> None:
    """Record script classes and exported property declarations for later linking."""
    try:
        from tree_sitter import Language, Parser

        grammar = importlib.import_module("tree_sitter_gdscript")
        tree = Parser(Language(grammar.language())).parse(source)
    except Exception as exc:
        result["error"] = f"tree-sitter-gdscript failed to parse {path.name}: {exc}"
        result["nodes"] = []
        result["edges"] = []
        return

    root = tree.root_node
    file_node = next(
        (
            node for node in result.get("nodes", [])
            if node.get("id") == _make_id(str(path))
        ),
        None,
    )
    if file_node is None:
        return

    class_name_node = next(
        (node for node in root.named_children if node.type == "class_name_statement"),
        None,
    )
    class_name = (
        _node_text(class_name_node.child_by_field_name("name"), source)
        if class_name_node else ""
    )
    extends_node = next(
        (node for node in root.named_children if node.type == "extends_statement"),
        None,
    )
    base_node = extends_node.child_by_field_name("type") if extends_node else None
    if (
        base_node is None
        and extends_node is not None
        and extends_node.named_children
    ):
        base_node = extends_node.named_children[0]
    base_type = _node_text(base_node, source)
    root_annotations = [
        _node_text(node, source)[:160]
        for node in root.named_children
        if node.type == "annotation"
    ]

    stem = _file_stem(path)
    project_context = godot_project_context(path, scan_root)
    script_uid = read_uid_sidecar(path)
    display_name = class_name or path.stem
    script_class_id = _make_id(stem, "script_class", display_name)
    class_line = (class_name_node or root).start_point[0] + 1
    class_metadata = {
        "godot_kind": "script_class",
        "godot_global_class": bool(class_name),
        "godot_project_id": project_context["project_id"],
        "godot_res_path": project_context["res_path"],
    }
    if script_uid:
        class_metadata["godot_uid"] = script_uid
    if base_type:
        class_metadata["godot_base_type"] = base_type[:200]

    result["nodes"].append({
        "id": script_class_id,
        "label": display_name,
        "file_type": "code",
        "source_file": file_node["source_file"],
        "source_location": f"L{class_line}",
        "metadata": class_metadata,
    })
    result["edges"].append({
        "source": file_node["id"],
        "target": script_class_id,
        "relation": "contains",
        "confidence": "EXTRACTED",
        "source_file": file_node["source_file"],
        "source_location": f"L{class_line}",
        "weight": 1.0,
    })

    file_metadata = dict(file_node.get("metadata") or {})
    file_metadata.update({
        "godot_kind": "file",
        "godot_project_id": project_context["project_id"],
        "godot_res_path": project_context["res_path"],
    })
    if script_uid:
        file_metadata["godot_uid"] = script_uid
    script_metadata = {"class_label": display_name, "global_class": bool(class_name)}
    if class_name:
        script_metadata["class_name"] = class_name
    if base_type:
        script_metadata["extends"] = base_type[:200]
    if root_annotations:
        script_metadata["annotations"] = root_annotations
    resource_refs, class_refs = _script_reference_facts(root, source)
    if resource_refs:
        script_metadata["resource_references"] = resource_refs
    if class_refs:
        script_metadata["class_references"] = class_refs
    file_metadata["godot_script"] = script_metadata
    file_node["metadata"] = file_metadata

    # The generic extractor sees file-level methods as module functions. Reparent
    # them to the script class and give their IDs a class-scoped owner.
    remapped: dict[str, str] = {}
    for edge in result.get("edges", []):
        if edge.get("source") != file_node["id"] or edge.get("relation") != "contains":
            continue
        target_id = edge.get("target")
        method = next(
            (node for node in result["nodes"] if node.get("id") == target_id),
            None,
        )
        label = method.get("label", "") if method else ""
        if not label.endswith("()"):
            continue
        method_name = label[:-2]
        new_id = _make_id(script_class_id, method_name)
        remapped[target_id] = new_id
        method["id"] = new_id
        edge["source"] = script_class_id
        edge["target"] = new_id
        edge["relation"] = "method"

    if remapped:
        for edge in result.get("edges", []):
            edge["source"] = remapped.get(edge.get("source"), edge.get("source"))
            edge["target"] = remapped.get(edge.get("target"), edge.get("target"))
        for raw_call in result.get("raw_calls", []):
            caller = raw_call.get("caller_nid")
            if caller in remapped:
                raw_call["caller_nid"] = remapped[caller]

    for declaration in root.named_children:
        if declaration.type != "variable_statement":
            continue
        name = _node_text(declaration.child_by_field_name("name"), source)
        if not name:
            continue
        annotations_node = declaration.child_by_field_name("annotations")
        if annotations_node is None:
            annotations_node = next(
                (child for child in declaration.named_children if child.type == "annotations"),
                None,
            )
        annotation_text = _node_text(annotations_node, source)[:240]
        type_text = _node_text(declaration.child_by_field_name("type"), source)[:200]
        if type_text == ":=":
            type_text = ""
        property_id = _make_id(script_class_id, "property", name)
        property_metadata = {
            "godot_kind": "script_property",
            "godot_exported": bool(re.search(r"@export(?:_[A-Za-z0-9_]+)?\b", annotation_text)),
        }
        if annotation_text:
            property_metadata["godot_annotations"] = annotation_text
        if type_text:
            property_metadata["godot_declared_type"] = type_text
        result["nodes"].append({
            "id": property_id,
            "label": name,
            "file_type": "code",
            "source_file": file_node["source_file"],
            "source_location": f"L{declaration.start_point[0] + 1}",
            "metadata": property_metadata,
        })
        result["edges"].append({
            "source": script_class_id,
            "target": property_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "source_file": file_node["source_file"],
            "source_location": f"L{declaration.start_point[0] + 1}",
            "weight": 1.0,
        })

    if root.has_error:
        first_error_line = result.get("parse_errors", {}).get("first_error_line")
        if not first_error_line:
            first_error_line = root.start_point[0] + 1
        script_metadata["parser_diagnostics"] = [{
            "kind": "godot_parse_error",
            "line": first_error_line,
            "message": "GDScript grammar recovered from syntax it did not parse; graph data may be partial.",
        }]
        file_node["metadata"]["godot_script"] = script_metadata
        result.setdefault("diagnostics", []).append({
            "kind": "godot_parse_error",
            "message": "GDScript grammar recovered from syntax it did not parse; graph data may be partial.",
            "source_file": file_node["source_file"],
            "source_location": f"L{first_error_line}",
        })


def extract_gdscript(path: Path, *, root: Path | None = None) -> dict:
    """Extract GDScript functions, script class structure, and top-level properties."""
    try:
        source = _read_source_bytes(path)
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}

    result = _extract_generic(
        path,
        _GDSCRIPT_CONFIG,
        source_override=source,
        scan_root=root,
    )
    if not result.get("error"):
        _add_script_metadata(path, source, result, root)
    return result
