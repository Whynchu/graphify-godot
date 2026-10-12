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
_GODOT_SOURCE_SUFFIXES = frozenset({".gd", ".tres", ".tscn", ".godot"})
_INLINE_COMPOUND_HEADER_RE = re.compile(r"^\s*(?:(?:static)\s+)*func\b|^\s*(?:if|elif|else|for|while|match|class)\b")


def _node_text(node, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _call_expression_fact(node, source: bytes) -> dict | None:
    """Return a small syntax-backed receiver/method record for a call value."""
    if node is None:
        return None
    if node.type == "call":
        parts = node.named_children
        if parts and parts[0].type == "identifier":
            return {
                "receiver": "",
                "receiver_kind": "unqualified",
                "callee": _node_text(parts[0], source)[:160],
            }
    if node.type == "attribute":
        parts = node.named_children
        if len(parts) < 2 or parts[-1].type != "attribute_call":
            return None
        call_parts = parts[-1].named_children
        if not call_parts or call_parts[0].type != "identifier":
            return None
        receiver = ".".join(_node_text(part, source) for part in parts[:-1])
        if receiver == "self":
            receiver_kind = "self"
        elif receiver == "super":
            receiver_kind = "super"
        elif receiver[:1].isupper() and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", receiver):
            receiver_kind = "class"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", receiver):
            receiver_kind = "identifier"
        else:
            receiver_kind = "dynamic"
        return {
            "receiver": receiver[:160],
            "receiver_kind": receiver_kind,
            "callee": _node_text(call_parts[0], source)[:160],
        }
    return None


def _normalize_inline_compound_statements(source: bytes) -> tuple[bytes, list[int], int]:
    """Put supported one-line compound suites on indented parser lines.

    Godot accepts compact forms such as ``if ready: return`` and
    ``func tick(): if ready: update()``. The bundled grammar is more reliable
    with the equivalent multiline form. This source-preserving transform skips
    comments and quoted strings and maps every inserted parser line back to its
    original source line.
    """
    out = bytearray()
    parser_line_to_source_line = [1]
    source_line = 1
    output_line_start = 0
    index = 0
    quote = 0
    triple = False
    escaped = False
    comment = False
    nesting = 0
    splits = 0
    while index < len(source):
        char = source[index]
        if char == 10:
            out.append(char)
            source_line += 1
            parser_line_to_source_line.append(source_line)
            output_line_start = len(out)
            comment = False
            index += 1
            continue
        if comment:
            out.append(char)
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif triple and source[index:index + 3] == bytes((quote, quote, quote)):
                out.extend(source[index:index + 3])
                index += 3
                quote = 0
                triple = False
                continue
            elif not triple and char == quote:
                quote = 0
            out.append(char)
            index += 1
            continue
        if char == 35:
            comment = True
            out.append(char)
            index += 1
            continue
        if char in (34, 39):
            quote = char
            triple = source[index:index + 3] == bytes((char, char, char))
            length = 3 if triple else 1
            out.extend(source[index:index + length])
            index += length
            continue
        if char in (40, 91, 123):  # ( [ {
            nesting += 1
        elif char in (41, 93, 125):  # ) ] }
            nesting = max(0, nesting - 1)
        if char == 58 and nesting == 0:
            prefix = out[output_line_start:].decode("utf-8", errors="replace")
            remainder = source[index + 1:].split(b"\n", 1)[0].strip(b" \t\r")
            if _INLINE_COMPOUND_HEADER_RE.match(prefix) and remainder and not remainder.startswith(b"#"):
                out.append(char)
                out.append(10)
                parser_line_to_source_line.append(source_line)
                indent = prefix[:len(prefix) - len(prefix.lstrip(" \t"))]
                out.extend(indent.encode("utf-8") + b"\t")
                output_line_start = len(out) - len(indent.encode("utf-8")) - 1
                splits += 1
                index += 1
                continue
        out.append(char)
        index += 1
    return bytes(out), parser_line_to_source_line, splits


def _normalize_semicolon_statements(source: bytes) -> tuple[bytes, list[int], int]:
    """Split statement separators for the parser and retain original line numbers.

    GDScript permits multiple statements on a line, while the bundled grammar
    currently recovers on some such lines. This whitespace-only normalization
    lets Tree-sitter see ordinary block statements without losing the original
    source locations in the final graph. Strings, multiline strings, and
    comments are left intact.
    """
    out = bytearray()
    parser_line_to_source_line = [1]
    source_line = 1
    line_start = 0
    index = 0
    quote = 0
    triple = False
    escaped = False
    comment = False
    splits = 0
    while index < len(source):
        char = source[index]
        if char == 10:
            out.append(char)
            source_line += 1
            parser_line_to_source_line.append(source_line)
            line_start = index + 1
            comment = False
            index += 1
            continue
        if comment:
            out.append(char)
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif triple and source[index:index + 3] == bytes((quote, quote, quote)):
                out.extend(source[index:index + 3])
                index += 3
                quote = 0
                triple = False
                continue
            elif not triple and char == quote:
                quote = 0
            out.append(char)
            index += 1
            continue
        if char == 35:  # # comment
            comment = True
            out.append(char)
            index += 1
            continue
        if char in (34, 39):  # single, double, or triple-quoted string
            quote = char
            triple = source[index:index + 3] == bytes((char, char, char))
            length = 3 if triple else 1
            out.extend(source[index:index + length])
            index += length
            continue
        if char == 59:  # ; outside strings/comments
            cursor = index + 1
            while cursor < len(source) and source[cursor] in (9, 32):
                cursor += 1
            if cursor < len(source) and source[cursor] not in (10, 13, 35):
                indent_end = line_start
                while indent_end < index and source[indent_end] in (9, 32):
                    indent_end += 1
                out.append(10)
                parser_line_to_source_line.append(source_line)
                out.extend(source[line_start:indent_end])
                index = cursor
                splits += 1
                continue
        out.append(char)
        index += 1
    return bytes(out), parser_line_to_source_line, splits


def _restore_original_script_lines(value, parser_line_to_source_line: list[int], key: str = "") -> None:
    """Translate parser locations back after semicolon-line normalization."""
    def mapped(line: int) -> int:
        if line <= 0:
            return line
        return parser_line_to_source_line[min(line - 1, len(parser_line_to_source_line) - 1)]

    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key == "source_location" and isinstance(child, str):
                match = re.fullmatch(r"L(\d+)", child)
                if match:
                    value[child_key] = f"L{mapped(int(match.group(1)))}"
            elif (child_key == "line" or child_key.endswith("_line") or child_key in {"first_error_line", "last_error_line"}) and isinstance(child, int):
                value[child_key] = mapped(child)
            else:
                _restore_original_script_lines(child, parser_line_to_source_line, child_key)
    elif isinstance(value, list):
        for child in value:
            _restore_original_script_lines(child, parser_line_to_source_line, key)


def _node_path_reference(node, source: bytes) -> dict | None:
    """Return a syntax-backed `$`, `%`, or literal `get_node` reference."""
    syntax = ""
    path = ""
    dynamic = False
    if node.type == "get_node":
        raw = _node_text(node, source).strip()
        if raw.startswith("$"):
            syntax, value = "dollar", raw[1:].strip()
        elif raw.startswith("%"):
            syntax, value = "unique", raw[1:].strip()
        else:
            return None
        decoded = _decode_string(value)
        path = decoded if decoded is not None else value
    elif node.type == "call":
        named = node.named_children
        if not named or named[0].type != "identifier" or _node_text(named[0], source) != "get_node":
            return None
        arguments = next((item for item in named[1:] if item.type == "arguments"), None)
        argument = arguments.named_children[0] if arguments and arguments.named_children else None
        syntax = "get_node"
        if argument is None:
            dynamic = True
        else:
            value = _node_text(argument, source).strip()
            if argument.type == "node_path" and value.startswith("^"):
                value = value[1:].strip()
            decoded = _decode_string(value)
            if decoded is None:
                dynamic = True
            else:
                path = decoded
    else:
        return None
    return {"syntax": syntax, "path": path[:500], "dynamic": dynamic}


def _script_reference_facts(root, source: bytes) -> tuple[list[dict], list[dict], list[dict]]:
    """Collect bounded, syntax-backed script dependencies and class references.

    Resource references are limited to literal ``preload``/``load`` calls whose
    targets are Godot source resources. Class references are collected only in
    type, constructor, inheritance, and attribute-receiver positions; the
    project resolver later keeps names that match a unique ``class_name``.
    """
    resources: list[dict] = []
    class_refs: list[dict] = []
    dynamic_refs: list[dict] = []
    resource_seen: set[tuple[str, str, int, str]] = set()
    class_seen: set[tuple[str, str, int]] = set()
    lexical_names: set[str] = set()
    root_class_decl = next((item for item in root.named_children if item.type == "class_name_statement"), None)
    root_class_line = (root_class_decl or root).start_point[0] + 1

    binding_stack = [root]
    while binding_stack:
        node = binding_stack.pop()
        binding_stack.extend(node.named_children)
        if node.type == "variable_statement":
            declared = _node_text(node.child_by_field_name("name"), source)
            if declared:
                lexical_names.add(declared)
        elif node.type == "const_statement":
            declared = _node_text(node.child_by_field_name("name"), source)
            if declared:
                lexical_names.add(declared)
        elif node.type == "function_definition":
            parameters = node.child_by_field_name("parameters")
            if parameters:
                for parameter in parameters.named_children:
                    name_node = parameter.child_by_field_name("name")
                    if name_node is None and parameter.named_children:
                        name_node = parameter.named_children[0]
                    name = _node_text(name_node, source)
                    if name and name_node.type in {"identifier", "name"}:
                        lexical_names.add(name)

    def const_binding(node) -> tuple[str, int]:
        parent = node.parent
        while parent is not None:
            if parent.type == "const_statement":
                binding = _node_text(parent.child_by_field_name("name"), source)[:160]
                owner = parent.parent
                while owner is not None and owner.type not in {"class_definition", "module"}:
                    owner = owner.parent
                owner_line = owner.start_point[0] + 1 if owner is not None and owner.type == "class_definition" else root_class_line
                return binding, owner_line
            if parent.type in {"function_definition", "class_definition"}:
                break
            parent = parent.parent
        return "", 1

    def resource_binding(node) -> tuple[str, int]:
        parent = node.parent
        if parent is not None and parent.type == "variable_statement":
            value_node = parent.child_by_field_name("value")
            if value_node is not None and value_node.start_byte <= node.start_byte < value_node.end_byte:
                binding = _node_text(parent.child_by_field_name("name"), source)[:160]
                owner = parent.parent
                while owner is not None and owner.type not in {"class_definition", "module"}:
                    owner = owner.parent
                owner_line = owner.start_point[0] + 1 if owner is not None and owner.type == "class_definition" else root_class_line
                return binding, owner_line
        return const_binding(node)

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
        binding, owner_class_line = resource_binding(call_node)
        key = (kind, path, line, binding)
        if key in resource_seen or len(resources) >= _MAX_SCRIPT_RESOURCE_REFS:
            return
        resource_seen.add(key)
        resources.append({
            "kind": kind, "path": path[:500], "line": line, "binding": binding,
            "owner_class_line": owner_class_line,
        })

    def add_dynamic_resource(kind: str, call_node, argument_node) -> None:
        if argument_node is not None and argument_node.type == "string":
            return
        binding, owner_class_line = const_binding(call_node)
        dynamic_refs.append({
            "kind": "computed_resource_load",
            "operation": kind,
            "binding": binding,
            "owner_class_line": owner_class_line,
            "line": call_node.start_point[0] + 1,
        })

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
                    add_dynamic_resource(kind, node, argument)

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
                add_dynamic_resource("ResourceLoader.load", attribute, argument)

        if node.type != "identifier":
            continue
        name = _node_text(node, source)
        parent = node.parent
        if not name or parent is None:
            continue
        ref_kind = ""
        if parent.type == "type":
            ref_kind = "extends" if parent.parent is not None and parent.parent.type == "extends_statement" else "type"
        elif (
            parent.type == "attribute"
            and parent.named_children
            and parent.named_children[0].id == node.id
            and name not in lexical_names
            and name not in {"self", "super"}
        ):
            ref_kind = "class_receiver"
        elif (
            parent.type == "call"
            and parent.parent is not None
            and parent.parent.type != "attribute"
            and parent.named_children
            and parent.named_children[0].id == node.id
            and name[:1].isupper()
        ):
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
    return resources, class_refs, dynamic_refs


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
        "godot_qualified_class_name": (class_name or stem)[:240],
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
    resource_refs, class_refs, dynamic_resource_refs = _script_reference_facts(root, source)
    if resource_refs:
        script_metadata["resource_references"] = resource_refs
    if class_refs:
        script_metadata["class_references"] = class_refs
    if dynamic_resource_refs:
        script_metadata["dynamic_references"] = dynamic_resource_refs[:50_000]
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

    classes_by_location = {
        (node.get("source_location"), str(node.get("label") or "")): node
        for node in result["nodes"]
        if node.get("source_file") == file_node["source_file"]
        and not str(node.get("label") or "").endswith("()")
    }
    methods_by_location = {
        (node.get("source_location"), str(node.get("label") or "").strip().lstrip(".").removesuffix("()")): node
        for node in result["nodes"]
        if str(node.get("label") or "").endswith("()") or str(node.get("label") or "").startswith(".")
    }
    call_references: list[dict] = []
    receiver_bindings: list[dict] = []
    property_references: list[dict] = []
    signal_references: list[dict] = []
    node_references: list[dict] = []
    dynamic_references: list[dict] = list(dynamic_resource_refs)
    declarations: set[tuple[str, str, str]] = set()
    member_ids: set[str] = {str(node.get("id") or "") for node in result.get("nodes", [])}

    def add_member(owner_id: str, name: str, kind: str, declaration, extra: dict | None = None) -> dict | None:
        if not name or (owner_id, kind, name) in declarations:
            return None
        declarations.add((owner_id, kind, name))
        line = declaration.start_point[0] + 1
        node_id = _make_id(owner_id, kind, name)
        if node_id in member_ids:
            # make_id() trims identifier-edge underscores for readability, so
            # valid GDScript names such as ``health`` and ``_health`` can
            # otherwise collapse to the same graph ID. Keep the common ID
            # unchanged and add an encoded source name only for collisions.
            raw_name = name.encode("utf-8").hex()
            node_id = f"{node_id}_symbol_{raw_name}"
            suffix = 2
            while node_id in member_ids:
                node_id = f"{_make_id(owner_id, kind, name)}_symbol_{raw_name}_{suffix}"
                suffix += 1
        member_ids.add(node_id)
        metadata = {"godot_kind": kind}
        metadata.update({
            "godot_project_id": project_context["project_id"],
            "godot_res_path": project_context["res_path"],
        })
        if extra:
            metadata.update(extra)
        member = {
            "id": node_id,
            "label": name,
            "file_type": "code",
            "source_file": file_node["source_file"],
            "source_location": f"L{line}",
            "metadata": metadata,
        }
        result["nodes"].append(member)
        result["edges"].append({
            "source": owner_id,
            "target": node_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "source_file": file_node["source_file"],
            "source_location": f"L{line}",
            "weight": 1.0,
        })
        return member

    def local_names_for(function_node) -> set[str]:
        names: set[str] = set()
        stack = [function_node]
        while stack:
            current = stack.pop()
            if current is not function_node and current.type in {"function_definition", "class_definition", "lambda"}:
                continue
            if current.type == "variable_statement":
                value = _node_text(current.child_by_field_name("name"), source)
                if value:
                    names.add(value)
            if current.type in {"typed_parameter", "default_parameter", "parameter"}:
                name_node = current.child_by_field_name("name")
                if name_node is None:
                    name_node = next((child for child in current.named_children if child.type in {"identifier", "name"}), None)
                value = _node_text(name_node, source)
                if value:
                    names.add(value)
            stack.extend(current.named_children)
        return names

    def visit_class(class_ast, owner_id: str, owner_line: int, owner_qualified_name: str) -> None:
        declarations_in_class = class_ast.named_children
        property_names = {
            _node_text(child.child_by_field_name("name"), source)
            for child in declarations_in_class if child.type == "variable_statement"
        }
        property_names.discard("")
        signal_names = {
            _node_text(child.child_by_field_name("name"), source)
            for child in declarations_in_class if child.type == "signal_statement"
        }
        signal_names.discard("")
        for declaration in declarations_in_class:
            if declaration.type == "class_body":
                visit_class(declaration, owner_id, top_level)
            elif declaration.type == "class_definition":
                class_name_node = declaration.child_by_field_name("name")
                class_name = _node_text(class_name_node, source)
                class_node = classes_by_location.get((f"L{declaration.start_point[0] + 1}", class_name))
                if class_node is None:
                    continue
                class_metadata = dict(class_node.get("metadata") or {})
                class_metadata.update({
                    "godot_kind": "script_inner_class",
                    "godot_qualified_class_name": f"{owner_qualified_name}.{class_name}"[:240],
                    "godot_owner_class_line": owner_line,
                    "godot_project_id": project_context["project_id"],
                    "godot_res_path": project_context["res_path"],
                })
                class_node["metadata"] = class_metadata
                for edge in result["edges"]:
                    if edge.get("target") == class_node["id"] and edge.get("relation") == "contains" and edge.get("source") == file_node["id"]:
                        edge["source"] = owner_id
                visit_class(
                    declaration.child_by_field_name("body"),
                    class_node["id"],
                    declaration.start_point[0] + 1,
                    f"{owner_qualified_name}.{class_name}",
                )
            elif declaration.type == "function_definition":
                name = _node_text(declaration.child_by_field_name("name"), source)
                method = methods_by_location.get((f"L{declaration.start_point[0] + 1}", name))
                if method:
                    metadata = dict(method.get("metadata") or {})
                    metadata.update({
                        "godot_parameters": _node_text(declaration.child_by_field_name("parameters"), source)[:500],
                        "godot_static": bool(re.search(r"\bstatic\s+func\b", _node_text(declaration, source)[:120])),
                        "godot_owner_class_line": owner_line,
                    })
                    return_type = _node_text(declaration.child_by_field_name("return_type"), source)
                    if return_type:
                        metadata["godot_return_type"] = return_type[:200]
                    annotation_text = " ".join(
                        _node_text(child, source) for child in declaration.named_children if child.type == "annotation"
                    )[:240]
                    if annotation_text:
                        metadata["godot_annotations"] = annotation_text
                    method["metadata"] = metadata

                    parameters = declaration.child_by_field_name("parameters")
                    if parameters:
                        for parameter in parameters.named_children:
                            param_name_node = parameter.child_by_field_name("name")
                            if param_name_node is None:
                                param_name_node = next((child for child in parameter.named_children if child.type in {"identifier", "name"}), None)
                            param_type = _node_text(parameter.child_by_field_name("type"), source)
                            param_name = _node_text(param_name_node, source)
                            if param_name:
                                receiver_bindings.append({
                                    "owner_class_line": owner_line,
                                    "caller_line": declaration.start_point[0] + 1,
                                    "name": param_name[:160], "type": param_type[:200],
                                    "line": parameter.start_point[0] + 1, "inferred": False,
                                    "declared": bool(param_type), "unknown": not bool(param_type),
                                })

                    local_names = local_names_for(declaration)
                    body = declaration.child_by_field_name("body")
                    stack = [(body, None)] if body is not None else []
                    while stack:
                        call_node, lambda_scope = stack.pop()
                        if call_node is not body and call_node.type in {"function_definition", "class_definition"}:
                            continue
                        if call_node.type == "lambda":
                            lambda_line = call_node.start_point[0] + 1
                            lambda_column = call_node.start_point[1] + 1
                            lambda_label = f"lambda@L{lambda_line}:{lambda_column}"
                            lambda_id = _make_id(
                                owner_id,
                                "script_lambda",
                                f"L{declaration.start_point[0] + 1}",
                                f"L{lambda_line}",
                                f"C{lambda_column}",
                            )
                            lambda_node = {
                                "id": lambda_id,
                                "label": lambda_label[:180],
                                "file_type": "code",
                                "source_file": file_node["source_file"],
                                "source_location": f"L{lambda_line}",
                                "metadata": {
                                    "godot_kind": "script_lambda",
                                    "godot_owner_class_line": owner_line,
                                    "godot_enclosing_method_line": declaration.start_point[0] + 1,
                                    "godot_enclosing_method_name": name[:160],
                                    "godot_lambda_column": lambda_column,
                                    "godot_project_id": project_context["project_id"],
                                    "godot_res_path": project_context["res_path"],
                                },
                            }
                            result["nodes"].append(lambda_node)
                            member_ids.add(lambda_id)
                            parent_lambda = lambda_scope["node"] if lambda_scope else method
                            if parent_lambda is not None:
                                result["edges"].append({
                                    "source": parent_lambda["id"],
                                    "target": lambda_id,
                                    "relation": "contains",
                                    "context": "godot_lambda",
                                    "confidence": "EXTRACTED",
                                    "source_file": file_node["source_file"],
                                    "source_location": f"L{lambda_line}",
                                    "weight": 1.0,
                                })
                            lambda_scope = {
                                "line": lambda_line,
                                "column": lambda_column,
                                "name": lambda_label,
                                "node": lambda_node,
                                "local_names": local_names_for(call_node),
                            }
                            lambda_parameters = call_node.child_by_field_name("parameters")
                            if lambda_parameters is not None:
                                for parameter in lambda_parameters.named_children:
                                    param_name_node = parameter.child_by_field_name("name")
                                    if param_name_node is None:
                                        param_name_node = next((child for child in parameter.named_children if child.type in {"identifier", "name"}), None)
                                    param_name = _node_text(param_name_node, source)
                                    if param_name:
                                        param_type = _node_text(parameter.child_by_field_name("type"), source)
                                        receiver_bindings.append({
                                            "owner_class_line": owner_line,
                                            "caller_line": lambda_line,
                                            "caller_kind": "lambda",
                                            "caller_column": lambda_column,
                                            "name": param_name[:160],
                                            "type": param_type[:200],
                                            "line": parameter.start_point[0] + 1,
                                            "inferred": False,
                                            "declared": bool(param_type),
                                            "unknown": not bool(param_type),
                                        })
                            lambda_body = call_node.child_by_field_name("body")
                            if lambda_body is not None:
                                stack.append((lambda_body, lambda_scope))
                            continue
                        caller_line = lambda_scope["line"] if lambda_scope else declaration.start_point[0] + 1
                        caller_name = lambda_scope["name"] if lambda_scope else name
                        caller_scope_fields = (
                            {
                                "caller_kind": "lambda",
                                "caller_column": lambda_scope["column"],
                                "caller_method_line": declaration.start_point[0] + 1,
                            }
                            if lambda_scope else {}
                        )
                        active_local_names = lambda_scope["local_names"] if lambda_scope else local_names
                        callee = ""
                        receiver = ""
                        receiver_kind = "unqualified"
                        receiver_indexed = False
                        receiver_indexed_depth = 0
                        call_line = call_node.start_point[0] + 1
                        path_fact = _node_path_reference(call_node, source)
                        if path_fact is not None and len(node_references) < 50_000:
                            binding_name = ""
                            parent = call_node.parent
                            if parent is not None and parent.type == "variable_statement":
                                value_node = parent.child_by_field_name("value")
                                if value_node is not None and value_node.id == call_node.id:
                                    binding_name = _node_text(parent.child_by_field_name("name"), source)
                            member_call = ""
                            if parent is not None and parent.type == "attribute":
                                children = parent.named_children
                                if len(children) == 2 and children[0].id == call_node.id and children[1].type == "attribute_call":
                                    call_parts = children[1].named_children
                                    if call_parts and call_parts[0].type == "identifier":
                                        member_call = _node_text(call_parts[0], source)
                            node_references.append({
                                **path_fact,
                                "caller_line": caller_line,
                                "caller_name": caller_name[:160],
                                **caller_scope_fields,
                                "owner_class_line": owner_line,
                                "binding": binding_name[:160],
                                "callee": member_call[:160],
                                "line": call_line,
                            })
                        if call_node.type == "call":
                            named = call_node.named_children
                            if named and named[0].type == "identifier":
                                callee = _node_text(named[0], source)
                        elif call_node.type == "attribute_call" and call_node.parent is not None and call_node.parent.type == "attribute":
                            attribute = call_node.parent
                            named = attribute.named_children
                            call_parts = call_node.named_children
                            if named and call_parts and call_parts[0].type == "identifier":
                                receiver_node = named[0]
                                receiver_indexed_depth = 0
                                if len(named) > 2:
                                    receiver = ".".join(_node_text(part, source) for part in named[:-1])
                                else:
                                    while receiver_node.type == "subscript" and receiver_node.named_children:
                                        receiver_indexed_depth += 1
                                        receiver_node = receiver_node.named_children[0]
                                    receiver = _node_text(receiver_node, source)
                                receiver_indexed = receiver_indexed_depth > 0
                                callee = _node_text(call_parts[0], source)
                                receiver_kind = "self" if receiver == "self" else "super" if receiver == "super" else "class" if receiver[:1].isupper() and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", receiver) else "identifier" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", receiver) else "dynamic"
                                signal_receiver = receiver
                                if receiver == "self" and len(named) > 2:
                                    signal_receiver = f"self.{_node_text(named[1], source)}"
                                signal_name = signal_receiver if signal_receiver in signal_names else signal_receiver.rsplit(".", 1)[-1] if signal_receiver.startswith("self.") else ""
                                may_be_inherited_signal = (
                                    callee in {"emit", "connect"}
                                    and (receiver == "self" or receiver.startswith("self."))
                                    and signal_name not in property_names
                                )
                                if (signal_name in signal_names or may_be_inherited_signal) and callee in {"emit", "connect"}:
                                    callback_name = ""
                                    callback_dynamic = False
                                    callback_receiver = ""
                                    callback_receiver_kind = "unqualified"
                                    callback_lambda_line = 0
                                    callback_lambda_column = 0
                                    if callee == "connect" and len(call_parts) > 1:
                                        arguments = call_parts[1]
                                        callback = arguments.named_children[0] if arguments.named_children else None
                                        if callback is not None and callback.type == "identifier":
                                            callback_name = _node_text(callback, source)
                                        elif callback is not None and callback.type == "attribute":
                                            callback_children = callback.named_children
                                            if len(callback_children) > 1:
                                                callback_name = _node_text(callback_children[-1], source)
                                                callback_receiver = _node_text(callback_children[0], source)
                                                callback_receiver_kind = "self" if callback_receiver == "self" else "identifier" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", callback_receiver) else "dynamic"
                                        elif callback is not None and callback.type == "call" and callback.named_children and _node_text(callback.named_children[0], source) == "Callable":
                                            callable_arguments = next((part for part in callback.named_children[1:] if part.type == "arguments"), None)
                                            callable_parts = callable_arguments.named_children if callable_arguments is not None else []
                                            if len(callable_parts) >= 2 and callable_parts[1].type == "string":
                                                callback_name = _decode_string(_node_text(callable_parts[1], source))
                                                callback_receiver = _node_text(callable_parts[0], source)
                                                callback_receiver_kind = (
                                                    "self" if callback_receiver == "self"
                                                    else "identifier" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", callback_receiver)
                                                    else "class" if callback_receiver[:1].isupper()
                                                    else "dynamic"
                                                )
                                        elif callback is not None and callback.type == "lambda":
                                            callback_lambda_line = callback.start_point[0] + 1
                                            callback_lambda_column = callback.start_point[1] + 1
                                        callback_dynamic = callback is not None and not callback_name and not callback_lambda_line
                                    signal_references.append({
                                        "caller_line": caller_line,
                                        "caller_name": caller_name[:160],
                                        **caller_scope_fields,
                                        "owner_class_line": owner_line,
                                        "signal": signal_name[:160],
                                        "event": "emit" if callee == "emit" else "connect",
                                        "callback": callback_name[:160],
                                        "callback_receiver": callback_receiver[:160],
                                        "callback_receiver_kind": callback_receiver_kind,
                                        "callback_lambda_line": callback_lambda_line,
                                        "callback_lambda_column": callback_lambda_column,
                                        "callback_dynamic": callback_dynamic,
                                        "line": call_line,
                                    })
                        call_parts = call_node.named_children
                        arguments_node = next((part for part in call_parts[1:] if part.type == "arguments"), None)
                        argument_nodes = arguments_node.named_children if arguments_node else []
                        first_argument = argument_nodes[0] if argument_nodes else None
                        first_string = _decode_string(_node_text(first_argument, source)) if first_argument is not None and first_argument.type == "string" else ""
                        dynamic_operations = {"call", "call_deferred", "callv", "call_group", "call_group_flags", "get_nodes_in_group", "set_script", "add_child"}
                        if callee in dynamic_operations and len(dynamic_references) < 50_000:
                            dynamic_references.append({
                                "kind": "dynamic_dispatch" if callee in {"call", "call_deferred", "callv"} else "runtime_group_lookup" if callee == "get_nodes_in_group" else "runtime_group_dispatch" if callee in {"call_group", "call_group_flags"} else "runtime_script_assignment" if callee == "set_script" else "runtime_node_injection",
                                "operation": callee,
                                "receiver": receiver[:160],
                                "receiver_kind": receiver_kind,
                                "literal_method": first_string[:160] if callee in {"call", "call_deferred"} else "",
                                "literal_group": first_string[:160] if callee in {"get_nodes_in_group", "call_group", "call_group_flags"} else "",
                                "owner_class_line": owner_line,
                                "caller_line": caller_line,
                                "caller_name": caller_name[:160],
                                **caller_scope_fields,
                                "line": call_line,
                            })
                        literal_property_access = callee in {"get", "set"} and bool(first_string)
                        if callee in {"get", "set"} and not first_string and len(dynamic_references) < 50_000:
                            dynamic_references.append({
                                "kind": "dynamic_property_access",
                                "operation": callee,
                                "receiver": receiver[:160],
                                "receiver_kind": receiver_kind,
                                "owner_class_line": owner_line,
                                "caller_line": caller_line,
                                "caller_name": caller_name[:160],
                                **caller_scope_fields,
                                "access": "read" if callee == "get" else "write",
                                "line": call_line,
                            })
                        elif literal_property_access:
                            property_references.append({
                                "caller_line": caller_line,
                                "caller_name": caller_name[:160],
                                **caller_scope_fields,
                                "owner_class_line": owner_line,
                                "property": first_string[:160],
                                "receiver": receiver[:160],
                                "receiver_kind": receiver_kind,
                                "access": "read" if callee == "get" else "write",
                                "dynamic_literal": True,
                                "line": call_line,
                            })
                        if callee in {"call", "call_deferred"} and first_string:
                            call_references.append({
                                "caller_line": caller_line,
                                "caller_name": caller_name[:160],
                                **caller_scope_fields,
                                "owner_class_line": owner_line,
                                "callee": first_string[:160],
                                "receiver": receiver[:160],
                                "receiver_kind": receiver_kind,
                                "shadowed": False,
                                "dynamic_dispatch": True,
                                "dispatch_operation": callee,
                                "line": call_line,
                            })
                        if callee and callee != "new" and callee not in {"call", "call_deferred", "callv", "call_group", "call_group_flags", "get_nodes_in_group", "set_script", "add_child", "get", "set"}:
                            call_references.append({
                                "caller_line": caller_line,
                                "caller_name": caller_name[:160],
                                **caller_scope_fields,
                                "owner_class_line": owner_line,
                                "callee": callee[:160],
                                "receiver": receiver[:160],
                                "receiver_kind": receiver_kind,
                                "receiver_indexed": receiver_indexed,
                                "receiver_indexed_depth": receiver_indexed_depth,
                                "shadowed": receiver_kind == "unqualified" and callee in active_local_names,
                                "line": call_line,
                            })
                        if call_node.type == "variable_statement":
                            var_name = _node_text(call_node.child_by_field_name("name"), source)
                            var_type = _node_text(call_node.child_by_field_name("type"), source)
                            value_node = call_node.child_by_field_name("value")
                            inferred_type = ""
                            if (not var_type or var_type == ":=") and value_node is not None and value_node.type == "attribute":
                                value_parts = value_node.named_children
                                if len(value_parts) >= 2 and _node_text(value_parts[0], source)[:1].isupper() and _node_text(value_parts[1], source).startswith("new"):
                                    inferred_type = _node_text(value_parts[0], source)
                            if var_name:
                                inferred_return_call = (
                                    _call_expression_fact(value_node, source)
                                    if not inferred_type and not (var_type and var_type != ":=")
                                    else None
                                )
                                receiver_bindings.append({
                                    "owner_class_line": owner_line, "caller_line": caller_line,
                                    **caller_scope_fields,
                                    "name": var_name[:160], "type": (inferred_type or (var_type if var_type != ":=" else ""))[:200],
                                    "line": call_line, "inferred": bool(inferred_type),
                                    "declared": bool(var_type and var_type != ":="),
                                    "unknown": not bool(inferred_type or (var_type and var_type != ":=")),
                                    **({"inferred_return_call": inferred_return_call} if inferred_return_call else {}),
                                })
                        if call_node.type == "identifier":
                            property_name = _node_text(call_node, source)
                            parent = call_node.parent
                            is_explicit_self_property = False
                            if parent is not None and parent.type == "attribute" and len(parent.named_children) > 1 and parent.named_children[1].id == call_node.id:
                                is_explicit_self_property = _node_text(parent.named_children[0], source) == "self"
                            is_attribute_receiver = parent is not None and parent.type == "attribute" and parent.named_children and parent.named_children[0].id == call_node.id
                            if property_name in property_names and property_name not in active_local_names and (not is_attribute_receiver or is_explicit_self_property):
                                access = "read"
                                ancestor = parent
                                while ancestor is not None and ancestor.type not in {"assignment", "augmented_assignment", "function_definition", "class_definition"}:
                                    ancestor = ancestor.parent
                                if ancestor is not None and ancestor.type in {"assignment", "augmented_assignment"}:
                                    left = ancestor.child_by_field_name("left")
                                    if left is not None and left.start_byte <= call_node.start_byte < left.end_byte:
                                        access = "read_write" if ancestor.type == "augmented_assignment" else "write"
                                property_references.append({
                                    "caller_line": caller_line,
                                    "caller_name": caller_name[:160],
                                    **caller_scope_fields,
                                    "owner_class_line": owner_line,
                                    "property": property_name[:160],
                                    "access": access,
                                    "line": call_line,
                                })
                        elif call_node.type == "attribute":
                            attribute_parts = call_node.named_children
                            if len(attribute_parts) == 2 and attribute_parts[1].type == "identifier":
                                receiver_node, property_node = attribute_parts
                                receiver_indexed_depth = 0
                                while receiver_node.type == "subscript" and receiver_node.named_children:
                                    receiver_indexed_depth += 1
                                    receiver_node = receiver_node.named_children[0]
                                receiver_name = _node_text(receiver_node, source)
                                property_name = _node_text(property_node, source)
                                if receiver_name != "self" and property_name:
                                    if receiver_name == "super":
                                        receiver_ref_kind = "super"
                                    elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", receiver_name):
                                        receiver_ref_kind = "class" if receiver_name[:1].isupper() else "identifier"
                                    else:
                                        receiver_ref_kind = "dynamic"
                                    access = "read"
                                    ancestor = call_node.parent
                                    while ancestor is not None and ancestor.type not in {"assignment", "augmented_assignment", "function_definition", "class_definition"}:
                                        ancestor = ancestor.parent
                                    if ancestor is not None and ancestor.type in {"assignment", "augmented_assignment"}:
                                        left = ancestor.child_by_field_name("left")
                                        if left is not None and left.start_byte <= call_node.start_byte < left.end_byte:
                                            access = "read_write" if ancestor.type == "augmented_assignment" else "write"
                                    property_references.append({
                                        "caller_line": caller_line,
                                        "caller_name": caller_name[:160],
                                        **caller_scope_fields,
                                        "owner_class_line": owner_line,
                                        "property": property_name[:160],
                                        "receiver": receiver_name[:160],
                                        "receiver_kind": receiver_ref_kind,
                                        "receiver_indexed": receiver_indexed_depth > 0,
                                        "receiver_indexed_depth": receiver_indexed_depth,
                                        "access": access,
                                        "line": call_line,
                                    })
                        stack.extend((child, lambda_scope) for child in reversed(call_node.named_children))
                    # Reassigning an inferred local changes the receiver value.
                    # Preserve a new type only for a straight-line, explicit
                    # Class.new() assignment; branch/loop assignments fail
                    # closed because the value after the block is uncertain.
                    assignment_stack = [body] if body is not None else []
                    assignments = []
                    while assignment_stack:
                        candidate = assignment_stack.pop()
                        if candidate.type == "lambda":
                            continue
                        if candidate is not body and candidate.type in {"function_definition", "class_definition"}:
                            continue
                        if candidate.type in {"assignment", "augmented_assignment"}:
                            assignments.append(candidate)
                        assignment_stack.extend(reversed(candidate.named_children))
                    for assignment in sorted(assignments, key=lambda item: (item.start_point[0], item.start_byte)):
                        left = assignment.child_by_field_name("left")
                        right = assignment.child_by_field_name("right")
                        if left is None or left.type != "identifier":
                            continue
                        reassigned_name = _node_text(left, source)
                        if not reassigned_name or reassigned_name not in local_names:
                            continue
                        line = assignment.start_point[0] + 1
                        prior = [
                            binding for binding in receiver_bindings
                            if binding.get("owner_class_line") == owner_line
                            and binding.get("caller_line") == declaration.start_point[0] + 1
                            and binding.get("name") == reassigned_name
                            and int(binding.get("line") or 0) < line
                        ]
                        if prior and max(prior, key=lambda binding: int(binding.get("line") or 0)).get("declared"):
                            continue
                        control_flow = {"if_statement", "elif_clause", "else_clause", "for_statement", "while_statement", "match_statement", "match_branch"}
                        parent = assignment.parent
                        conditional = False
                        while parent is not None and parent is not body:
                            if parent.type in control_flow:
                                conditional = True
                                break
                            parent = parent.parent
                        inferred_type = ""
                        if not conditional and assignment.type == "assignment" and right is not None and right.type == "attribute":
                            value_parts = right.named_children
                            if len(value_parts) >= 2 and _node_text(value_parts[0], source)[:1].isupper() and _node_text(value_parts[1], source).startswith("new"):
                                inferred_type = _node_text(value_parts[0], source)
                        receiver_bindings.append({
                            "owner_class_line": owner_line,
                            "caller_line": declaration.start_point[0] + 1,
                            "name": reassigned_name[:160],
                            "type": inferred_type[:200],
                            "line": line,
                            "inferred": bool(inferred_type),
                            "reassignment": True,
                            "invalidated": not bool(inferred_type),
                        })
            elif declaration.type == "variable_statement":
                name = _node_text(declaration.child_by_field_name("name"), source)
                annotations_node = declaration.child_by_field_name("annotations")
                if annotations_node is None:
                    annotations_node = next((child for child in declaration.named_children if child.type == "annotations"), None)
                annotation_text = _node_text(annotations_node, source)[:240]
                type_text = _node_text(declaration.child_by_field_name("type"), source)[:200]
                if type_text == ":=":
                    type_text = ""
                property_metadata = {
                    "godot_exported": bool(re.search(r"@export(?:_[A-Za-z0-9_]+)?\b", annotation_text)),
                    **({"godot_annotations": annotation_text} if annotation_text else {}),
                    **({"godot_declared_type": type_text} if type_text else {}),
                }
                accessors = declaration.child_by_field_name("setget")
                if accessors is not None:
                    for access_name, inline_node_type in (("setter", "set_body"), ("getter", "get_body")):
                        accessor = accessors.child_by_field_name("set" if access_name == "setter" else "get")
                        if accessor is None:
                            continue
                        if accessor.type == inline_node_type:
                            property_metadata[f"godot_{access_name}"] = "<inline>"
                        else:
                            property_metadata[f"godot_{access_name}"] = _node_text(accessor, source)[:160]
                    property_metadata["godot_accessor_style"] = (
                        "inline" if any(property_metadata.get(f"godot_{access_name}") == "<inline>" for access_name in ("setter", "getter"))
                        else "named"
                    )
                prop = add_member(owner_id, name, "script_property", declaration, property_metadata)
                if prop:
                    receiver_bindings.append({"owner_class_line": owner_line, "name": name[:160], "type": type_text, "line": declaration.start_point[0] + 1, "inferred": False, "declared": bool(type_text), "unknown": not bool(type_text)})
                    if accessors is not None:
                        for access_name, child_type in (("getter", "get_body"), ("setter", "set_body")):
                            accessor = accessors.child_by_field_name("get" if access_name == "getter" else "set")
                            if accessor is None or accessor.type != child_type:
                                continue
                            accessor_id = _make_id(prop["id"], "inline_accessor", access_name)
                            accessor_node = {
                                "id": accessor_id,
                                "label": f"{access_name} {name}"[:180],
                                "file_type": "code",
                                "source_file": file_node["source_file"],
                                "source_location": f"L{accessor.start_point[0] + 1}",
                                "metadata": {
                                    "godot_kind": "script_accessor",
                                    "godot_accessor": access_name,
                                    "godot_property": name[:160],
                                    "godot_owner_class_line": owner_line,
                                    "godot_project_id": project_context["project_id"],
                                    "godot_res_path": project_context["res_path"],
                                },
                            }
                            result["nodes"].append(accessor_node)
                            result["edges"].append({
                                "source": prop["id"],
                                "target": accessor_id,
                                "relation": "contains",
                                "context": "godot_inline_property_accessor",
                                "confidence": "EXTRACTED",
                                "source_file": file_node["source_file"],
                                "source_location": f"L{accessor.start_point[0] + 1}",
                                "weight": 1.0,
                            })
                            accessor_body = accessor.child_by_field_name("body")
                            accessor_stack = [accessor_body] if accessor_body is not None else []
                            while accessor_stack:
                                accessor_child = accessor_stack.pop()
                                if accessor_child.type == "identifier":
                                    referenced_name = _node_text(accessor_child, source)
                                    if referenced_name in property_names and referenced_name != name:
                                        access = "read"
                                        parent = accessor_child.parent
                                        while parent is not None and parent.type not in {"assignment", "augmented_assignment", "get_body", "set_body"}:
                                            parent = parent.parent
                                        if parent is not None and parent.type in {"assignment", "augmented_assignment"}:
                                            left = parent.child_by_field_name("left")
                                            if left is not None and left.start_byte <= accessor_child.start_byte < left.end_byte:
                                                access = "read_write" if parent.type == "augmented_assignment" else "write"
                                        property_references.append({
                                            "caller_accessor": access_name,
                                            "caller_accessor_property": name[:160],
                                            "owner_class_line": owner_line,
                                            "property": referenced_name[:160],
                                            "access": access,
                                            "line": accessor_child.start_point[0] + 1,
                                        })
                                accessor_stack.extend(reversed(accessor_child.named_children))
            elif declaration.type == "signal_statement":
                name = _node_text(declaration.child_by_field_name("name"), source)
                params = _node_text(declaration.child_by_field_name("parameters"), source)
                add_member(owner_id, name, "script_signal", declaration, {"godot_signature": params[:400]})
            elif declaration.type == "enum_definition":
                name = _node_text(declaration.child_by_field_name("name"), source)
                enum_node = add_member(owner_id, name, "script_enum", declaration)
                enum_body = declaration.child_by_field_name("body")
                if enum_node and enum_body:
                    for value_node in enum_body.named_children:
                        if value_node.type != "enumerator" or not value_node.named_children:
                            continue
                        value_name = _node_text(value_node.named_children[0], source)
                        value_text = _node_text(value_node, source)
                        add_member(enum_node["id"], value_name, "script_enum_value", value_node, {"godot_source_value": value_text[:120]})
            elif declaration.type == "const_statement":
                name = _node_text(declaration.child_by_field_name("name"), source)
                value_node = declaration.child_by_field_name("value")
                value_type = value_node.type if value_node is not None else ""
                add_member(owner_id, name, "script_constant", declaration, {"godot_value_kind": value_type})

    visit_class(root, script_class_id, class_line, class_name or stem)
    if call_references:
        script_metadata["call_references"] = call_references[:50_000]
    if receiver_bindings:
        script_metadata["receiver_bindings"] = receiver_bindings[:50_000]
    if property_references:
        script_metadata["property_references"] = property_references[:50_000]
    if signal_references:
        script_metadata["signal_references"] = signal_references[:50_000]
    if node_references:
        script_metadata["node_references"] = node_references[:50_000]
    if dynamic_references:
        script_metadata["dynamic_references"] = dynamic_references[:50_000]

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

    inline_source, inline_line_map, inline_split_count = _normalize_inline_compound_statements(source)
    parser_source, parser_to_inline_line_map, semicolon_split_count = _normalize_semicolon_statements(inline_source)
    parser_line_to_source_line = [
        inline_line_map[min(line - 1, len(inline_line_map) - 1)]
        for line in parser_to_inline_line_map
    ]
    result = _extract_generic(
        path,
        _GDSCRIPT_CONFIG,
        source_override=parser_source,
        scan_root=root,
    )
    if not result.get("error"):
        _add_script_metadata(path, parser_source, result, root)
        if inline_split_count or semicolon_split_count:
            file_node = next((node for node in result.get("nodes", []) if node.get("id") == _make_id(str(path))), None)
            if file_node is not None:
                metadata = file_node.setdefault("metadata", {}).setdefault("godot_script", {})
                metadata["parser_normalizations"] = {
                    "inline_compound_splits": inline_split_count,
                    "semicolon_statement_splits": semicolon_split_count,
                }
        _restore_original_script_lines(result, parser_line_to_source_line)
    return result
