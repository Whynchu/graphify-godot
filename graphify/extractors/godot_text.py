"""Bounded, non-evaluating extraction for Godot 4 text resources and scenes.

Godot serializes ``.tres`` and ``.tscn`` as sectioned text with Variant values.
This reader keeps that syntax deliberately small: it recognizes section headers,
assignments, balanced containers, and resource-reference constructors without
executing or importing authored data.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from graphify.extractors.base import _file_stem, _make_id, _read_source_bytes
from graphify.security import sanitize_metadata

_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_SECTIONS = 20_000
_MAX_PROPERTIES = 256
_MAX_REFERENCES = 2_000
_MAX_NODE_PATHS = 2_000
_MAX_CONNECTIONS = 2_000
_MAX_VALUE_LENGTH = 180
_UID_RE = re.compile(r"uid://[A-Za-z0-9_-]+")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCALAR_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_KNOWN_CONSTRUCTORS = frozenset({
    "AABB", "Array", "Basis", "Callable", "Color", "Dictionary", "NodePath",
    "Plane", "Projection", "Quaternion", "Rect2", "Rect2i", "Resource",
    "RID", "Signal", "StringName", "SubResource", "Transform2D", "Transform3D",
    "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i",
    "PackedByteArray", "PackedColorArray", "PackedFloat32Array", "PackedFloat64Array",
    "PackedInt32Array", "PackedInt64Array", "PackedStringArray", "PackedVector2Array",
    "PackedVector3Array", "PackedVector4Array", "ExtResource",
})


@dataclass
class _Section:
    kind: str
    attributes: dict[str, str]
    line: int
    properties: list[dict] = field(default_factory=list)


def godot_project_context(path: Path, root: Path | None = None) -> dict[str, str | None]:
    """Return portable project-relative identity for a Godot source file."""
    absolute = path.resolve()
    scan_root = Path(root).resolve() if root is not None else absolute.parent
    project_root: Path | None = None
    for parent in (absolute.parent, *absolute.parents):
        if (parent / "project.godot").is_file():
            project_root = parent
            break
        if parent == parent.parent:
            break
    if project_root is None:
        project_root = scan_root
    try:
        project_id = project_root.relative_to(scan_root).as_posix() or "."
    except ValueError:
        # A single-file scan may anchor below its owning project. The local
        # project still remains one resolution domain for that invocation.
        project_id = "."
    try:
        relative = absolute.relative_to(project_root).as_posix()
    except ValueError:
        relative = path.name
    return {
        "project_id": project_id,
        "res_path": "res://" + relative,
    }


def read_uid_sidecar(path: Path) -> str | None:
    """Read a Godot ``<file>.uid`` sidecar without treating it as source code."""
    sidecar = Path(str(path) + ".uid")
    try:
        if sidecar.stat().st_size > 2_048:
            return None
        match = _UID_RE.search(sidecar.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(0) if match else None


def _without_comment(line: str, comment_chars: str = "#") -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("\"", "'"):
            quote = char
        elif char in comment_chars:
            return line[:index]
    return line


def _scan_state(text: str) -> tuple[list[str], str]:
    stack: list[str] = []
    quote = ""
    escaped = False
    matching = {")": "(", "]": "[", "}": "{"}
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("\"", "'"):
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == matching[char]:
                stack.pop()
    return stack, quote


def _is_complete_value(text: str) -> bool:
    stack, quote = _scan_state(text)
    return not stack and not quote


def _header_end(text: str) -> int | None:
    if not text.startswith("["):
        return None
    stack: list[str] = ["["]
    quote = ""
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for index in range(1, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("\"", "'"):
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
                if not stack:
                    return index
    return None


def _read_token_value(text: str, start: int) -> tuple[str, int]:
    stack: list[str] = []
    quote = ""
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ("\"", "'"):
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if stack and stack[-1] == pairs[char]:
                stack.pop()
        elif char.isspace() and not stack:
            break
        index += 1
    return text[start:index].strip(), index


def _parse_pairs(text: str) -> dict[str, str]:
    """Parse whitespace-separated ``key=value`` header attributes."""
    pairs: dict[str, str] = {}
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        match = _IDENT_RE.match(text, index)
        if not match:
            break
        key = match.group(0)
        index = match.end()
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != "=":
            break
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        value, index = _read_token_value(text, index)
        pairs[key] = value
    return pairs


def _split_assignment(text: str) -> tuple[str, str] | None:
    quote = ""
    escaped = False
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("\"", "'"):
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}" and stack and stack[-1] == pairs[char]:
            stack.pop()
        elif char == "=" and not stack:
            key = text[:index].strip()
            if key and re.fullmatch(r"[^\s=]+", key):
                return key, text[index + 1:].strip()
            return None
    return None


def _decode_string(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith("&"):
        raw = raw[1:].lstrip()
    if len(raw) < 2 or raw[0] not in ("\"", "'") or raw[-1] != raw[0]:
        return None
    body = raw[1:-1]
    if raw[0] == '"':
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    replacements = {"\\n": "\n", "\\r": "\r", "\\t": "\t", "\\\\": "\\", "\\\"": '"', "\\'": "'"}
    for source, target in replacements.items():
        body = body.replace(source, target)
    return body


def _first_string_argument(text: str, open_paren: int) -> tuple[str | None, int]:
    index = open_paren + 1
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] not in ("\"", "'"):
        return None, index
    quote = text[index]
    start = index
    index += 1
    escaped = False
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            decoded = _decode_string(text[start:index + 1])
            return decoded, index + 1
        index += 1
    return None, index


def _find_resource_refs(raw: str) -> list[dict[str, str]]:
    """Find only recognized resource constructors, never reference-like strings."""
    found: list[dict[str, str]] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char in ("\"", "'"):
            quote = char
            index += 1
            escaped = False
            while index < len(raw):
                inner = raw[index]
                if escaped:
                    escaped = False
                elif inner == "\\":
                    escaped = True
                elif inner == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "#":
            newline = raw.find("\n", index)
            if newline < 0:
                break
            index = newline + 1
            continue
        match = _IDENT_RE.match(raw, index)
        if not match:
            index += 1
            continue
        name = match.group(0)
        cursor = match.end()
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
        if name not in {"ExtResource", "SubResource", "Resource"} or cursor >= len(raw) or raw[cursor] != "(":
            index = match.end()
            continue
        argument, after_argument = _first_string_argument(raw, cursor)
        if argument:
            if name == "ExtResource":
                found.append({"kind": "ext_resource", "id": argument})
            elif name == "SubResource":
                found.append({"kind": "sub_resource", "id": argument})
            elif argument.startswith(("res://", "uid://")):
                found.append({"kind": "path", "path": argument})
        index = after_argument if after_argument > cursor else match.end()
    return found


def _find_node_paths(raw: str) -> list[str]:
    """Find NodePath constructors outside strings and comments."""
    found: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char in ("\"", "'"):
            quote = char
            index += 1
            escaped = False
            while index < len(raw):
                inner = raw[index]
                if escaped:
                    escaped = False
                elif inner == "\\":
                    escaped = True
                elif inner == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "#":
            newline = raw.find("\n", index)
            if newline < 0:
                break
            index = newline + 1
            continue
        match = _IDENT_RE.match(raw, index)
        if not match:
            index += 1
            continue
        cursor = match.end()
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
        if match.group(0) != "NodePath" or cursor >= len(raw) or raw[cursor] != "(":
            index = match.end()
            continue
        argument, after_argument = _first_string_argument(raw, cursor)
        if argument is not None:
            found.append(argument)
        index = after_argument if after_argument > cursor else match.end()
    return found


def _constructor_name(raw: str) -> str | None:
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\s*\(", raw, re.S)
    return match.group(1) if match else None


def _count_items_from_delimiter(raw: str, open_index: int) -> int | None:
    if open_index < 0 or open_index >= len(raw) or raw[open_index] not in "([{":
        return None
    opening_for = {")": "(", "]": "[", "}": "{"}
    start_char = raw[open_index]
    stack = [start_char]
    quote = ""
    escaped = False
    item_count = 0
    segment_has_content = False
    for char in raw[open_index + 1:]:
        if quote:
            segment_has_content = True
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("\"", "'"):
            quote = char
            segment_has_content = True
        elif char in "([{":
            stack.append(char)
            segment_has_content = True
        elif char in ")]}":
            if stack and stack[-1] == opening_for[char]:
                stack.pop()
                if not stack:
                    return item_count + (1 if segment_has_content else 0)
        elif char == "," and len(stack) == 1:
            if segment_has_content:
                item_count += 1
            segment_has_content = False
        elif not char.isspace():
            segment_has_content = True
    return None


def _top_level_item_count(raw: str, constructor: str | None = None) -> int | None:
    """Count collection entries, ignoring commas nested in values and strings."""
    value = raw.strip()
    if value.startswith(("[", "{")):
        return _count_items_from_delimiter(value, 0)
    if not constructor:
        return None
    match = re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\s*\(", value, re.S)
    if not match:
        return None
    open_index = value.find("(", match.start(), match.end())
    if constructor in {"Array", "Dictionary"}:
        payload_index = open_index + 1
        while payload_index < len(value) and value[payload_index].isspace():
            payload_index += 1
        if payload_index < len(value) and value[payload_index] in "[{":
            return _count_items_from_delimiter(value, payload_index)
    return _count_items_from_delimiter(value, open_index)


def _summarize_value(raw: str) -> tuple[object, list[str]]:
    """Return bounded display metadata and diagnostics, never evaluate values."""
    value = raw.strip()
    if value.startswith("&\""):
        decoded_name = _decode_string(value)
        return {"type": "StringName", "value": (decoded_name or "")[:_MAX_VALUE_LENGTH]}, []
    decoded = _decode_string(value)
    if decoded is not None:
        return decoded[:_MAX_VALUE_LENGTH], []
    if value in {"true", "false"}:
        return value == "true", []
    if value in {"null", "nil"}:
        return None, []
    if _SCALAR_NUMBER_RE.fullmatch(value):
        try:
            return (int(value) if not any(c in value for c in ".eE") else float(value)), []
        except ValueError:
            pass
    refs = _find_resource_refs(value)
    constructor = _constructor_name(value)
    if constructor in {"ExtResource", "SubResource"}:
        return {"type": constructor, "id": refs[0].get("id", "") if refs else ""}, []
    if constructor == "Resource" and refs:
        return {"type": "Resource", "path": refs[0].get("path", "")}, []
    if constructor == "NodePath":
        argument, _ = _first_string_argument(value, value.find("("))
        return {"type": "NodePath", "value": (argument or "")[:_MAX_VALUE_LENGTH]}, []
    if constructor:
        summary: dict[str, object] = {"type": constructor}
        is_collection = (
            constructor in {"Array", "Dictionary"}
            or (constructor.startswith("Packed") and constructor.endswith("Array"))
        )
        if is_collection:
            count = _top_level_item_count(value, constructor)
            if count is not None:
                summary["size"] = count
        diagnostics = [] if constructor in _KNOWN_CONSTRUCTORS else [f"Unsupported constructor preserved opaquely: {constructor}"]
        return summary, diagnostics
    if value.startswith(("[", "{")):
        summary = {"type": "Array" if value.startswith("[") else "Dictionary"}
        count = _top_level_item_count(value)
        if count is not None:
            summary["size"] = count
        return summary, []
    return {"type": "opaque", "text": value[:_MAX_VALUE_LENGTH]}, []


def _parse_sections(text: str, *, project_settings: bool = False) -> tuple[list[_Section], list[dict]]:
    sections: list[_Section] = []
    diagnostics: list[dict] = []
    current: _Section | None = None
    pending_key: str | None = None
    pending_value = ""
    pending_line = 0
    for line_number, source_line in enumerate(text.splitlines(), start=1):
        clean = _without_comment(source_line, "#;" if project_settings else "#").strip()
        if not clean:
            if pending_key is not None:
                pending_value += "\n"
            continue
        if pending_key is None and clean.startswith("["):
            end = _header_end(clean)
            if end is None or clean[end + 1:].strip():
                diagnostics.append({"kind": "godot_text_parse_error", "line": line_number, "message": "Malformed section header."})
                continue
            header = clean[1:end].strip()
            match = _IDENT_RE.match(header)
            if not match:
                diagnostics.append({"kind": "godot_text_parse_error", "line": line_number, "message": "Section name is missing."})
                continue
            current = _Section(match.group(0), _parse_pairs(header[match.end():]), line_number)
            sections.append(current)
            if len(sections) > _MAX_SECTIONS:
                diagnostics.append({"kind": "godot_text_limit", "line": line_number, "message": "Section limit reached; remaining content was skipped."})
                break
            continue
        if pending_key is not None:
            pending_value += "\n" + clean
            if _is_complete_value(pending_value):
                if current is not None:
                    current.properties.append({"name": pending_key, "value": pending_value.strip(), "line": pending_line})
                pending_key = None
                pending_value = ""
            continue
        assignment = _split_assignment(clean)
        if assignment is None:
            diagnostics.append({"kind": "godot_text_parse_error", "line": line_number, "message": "Expected a key=value assignment or section header."})
            continue
        if current is None:
            if project_settings and assignment[0] == "config_version" and re.fullmatch(r"\d+", assignment[1].strip()):
                header = next((section for section in sections if section.kind == "__project_header__"), None)
                if header is None:
                    header = _Section("__project_header__", {}, line_number)
                    sections.insert(0, header)
                header.properties.append({"name": assignment[0], "value": assignment[1].strip(), "line": line_number})
                continue
            diagnostics.append({"kind": "godot_text_parse_error", "line": line_number, "message": "Property appears before the first section."})
            continue
        pending_key, pending_value = assignment
        pending_line = line_number
        if _is_complete_value(pending_value):
            current.properties.append({"name": pending_key, "value": pending_value, "line": pending_line})
            pending_key = None
            pending_value = ""
    if pending_key is not None:
        diagnostics.append({"kind": "godot_text_parse_error", "line": pending_line, "message": f"Unclosed value for property {pending_key!r}."})
    return sections, diagnostics


def _decoded_attr(section: _Section, name: str) -> str:
    value = section.attributes.get(name, "")
    decoded = _decode_string(value)
    return decoded if decoded is not None else value.strip()


def _reference_facts(raw: str, property_name: str, line: int, owner_key: str) -> list[dict]:
    facts = []
    for ref in _find_resource_refs(raw):
        context = (
            "godot_script_attachment" if property_name == "script"
            else "godot_scene_instance" if property_name == "instance"
            else "godot_resource_property"
        )
        item = {"owner_key": owner_key, "property": property_name[:120], "line": line, "context": context}
        if ref["kind"] == "path":
            item.update({"target_kind": "path", "target_path": ref["path"][:500]})
        else:
            item.update({"target_kind": ref["kind"], "target_id": ref["id"][:160]})
        facts.append(item)
    return facts


def _resource_name(path: Path, properties: list[dict]) -> str:
    for prop in properties:
        if prop["name"] == "resource_name":
            decoded = _decode_string(prop["value"])
            if decoded:
                return decoded[:120]
    return path.stem


def _source_location(line: int) -> str:
    return f"L{line}" if line > 0 else "L1"


def _add_edge(edges: list[dict], source: str, target: str, relation: str, source_file: str, line: int) -> None:
    edges.append({
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "EXTRACTED",
        "source_file": source_file,
        "source_location": _source_location(line),
        "weight": 1.0,
    })


def extract_godot_text(path: Path, *, root: Path | None = None) -> dict:
    """Extract authored structure from Godot 4 ``.tres`` and ``.tscn`` files."""
    try:
        raw = _read_source_bytes(path)
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}
    if len(raw) > _MAX_SOURCE_BYTES:
        return {"nodes": [], "edges": [], "error": f"Godot text resource exceeds {_MAX_SOURCE_BYTES} byte limit"}

    source_file = str(path)
    file_id = _make_id(source_file)
    kind = "scene" if path.suffix.lower() == ".tscn" else "resource"
    context = godot_project_context(path, root)
    source = raw.decode("utf-8", errors="replace")
    sections, diagnostics = _parse_sections(source)
    if not any(section.kind in ({"gd_scene"} if kind == "scene" else {"gd_resource"}) for section in sections):
        diagnostics.append({"kind": "godot_text_parse_error", "line": 1, "message": f"Missing {('gd_scene' if kind == 'scene' else 'gd_resource')} header."})

    header_kind = "gd_scene" if kind == "scene" else "gd_resource"
    main_header = next((section for section in sections if section.kind == header_kind), None)
    ext_resources: list[dict] = []
    sub_resources: list[dict] = []
    for section in sections:
        if section.kind == "ext_resource":
            local_id = _decoded_attr(section, "id")
            if not local_id:
                diagnostics.append({"kind": "godot_text_parse_error", "line": section.line, "message": "External resource is missing its local id."})
                continue
            ext_resources.append({
                "id": local_id[:160],
                "type": _decoded_attr(section, "type")[:120],
                "path": _decoded_attr(section, "path")[:500],
                "uid": _decoded_attr(section, "uid")[:160],
                "line": section.line,
            })
        elif section.kind == "sub_resource":
            local_id = _decoded_attr(section, "id")
            if not local_id:
                diagnostics.append({"kind": "godot_text_parse_error", "line": section.line, "message": "Subresource is missing its local id."})
                continue
            sub_resources.append({
                "id": local_id[:160],
                "type": _decoded_attr(section, "type")[:120],
                "line": section.line,
                "properties": section.properties[:_MAX_PROPERTIES],
            })

    scene_sections: list[tuple[_Section, str, str]] = []
    scene_paths: set[str] = set()
    if kind == "scene":
        root_seen = False
        for section in (item for item in sections if item.kind == "node"):
            name = _decoded_attr(section, "name") or f"Node{section.line}"
            parent = _decoded_attr(section, "parent")
            if not root_seen and parent in {"", "."}:
                full_path = "."
                root_seen = True
            elif parent in {"", "."}:
                full_path = name if not root_seen else name
                root_seen = True
            else:
                parent_path = parent.strip("/")
                full_path = f"{parent_path}/{name}" if parent_path and parent_path != "." else name
            if full_path in scene_paths:
                diagnostics.append({"kind": "godot_text_parse_error", "line": section.line, "message": f"Duplicate scene node path {full_path!r}."})
                continue
            scene_paths.add(full_path)
            scene_sections.append((section, full_path, parent))

    scene_id = _make_id(_file_stem(path), "godot_scene") if kind == "scene" else None
    resource_id = _make_id(_file_stem(path), "godot_resource") if kind == "resource" else None
    node_ids: dict[str, str] = {}
    local_ids: dict[str, str] = {}
    if scene_id:
        local_ids["scene"] = scene_id
    if resource_id:
        local_ids["resource"] = resource_id
    for sub in sub_resources:
        local_ids[f"sub:{sub['id']}"] = _make_id(_file_stem(path), "godot_sub_resource", sub["id"])
    for _section, full_path, _parent in scene_sections:
        node_ids[full_path] = _make_id(_file_stem(path), "godot_scene_node", full_path)
        local_ids[f"node:{full_path}"] = node_ids[full_path]

    references: list[dict] = []
    node_paths: list[dict] = []
    for section in sections:
        owner_key: str | None = None
        if section.kind == "resource":
            owner_key = "resource" if kind == "resource" else "scene"
        elif section.kind == "sub_resource":
            local_id = _decoded_attr(section, "id")
            if local_id:
                owner_key = f"sub:{local_id}"
        elif section.kind == "node":
            node_pair = next((pair for pair in scene_sections if pair[0] is section), None)
            if node_pair:
                owner_key = f"node:{node_pair[1]}"
                instance = section.attributes.get("instance", "")
                if instance:
                    references.extend(_reference_facts(instance, "instance", section.line, owner_key))
        if owner_key is None:
            continue
        for prop in section.properties:
            references.extend(_reference_facts(prop["value"], prop["name"], prop["line"], owner_key))
            if section.kind == "node" and len(node_paths) < _MAX_NODE_PATHS:
                node_paths.extend({
                    "owner_key": owner_key,
                    "property": prop["name"][:120],
                    "path": node_path[:500],
                    "line": prop["line"],
                } for node_path in _find_node_paths(prop["value"])[:_MAX_NODE_PATHS - len(node_paths)])
        if len(node_paths) >= _MAX_NODE_PATHS:
            diagnostics.append({"kind": "godot_text_limit", "line": section.line, "message": "NodePath-reference limit reached; remaining links were skipped."})
        if len(references) >= _MAX_REFERENCES:
            diagnostics.append({"kind": "godot_text_limit", "line": section.line, "message": "Resource-reference limit reached; remaining links were skipped."})
            references = references[:_MAX_REFERENCES]
            break

    connections = []
    for section in sections:
        if section.kind != "connection":
            continue
        connection = {
            "signal": _decoded_attr(section, "signal")[:160],
            "from": _decoded_attr(section, "from")[:500],
            "to": _decoded_attr(section, "to")[:500],
            "method": _decoded_attr(section, "method")[:160],
            "line": section.line,
        }
        if connection["signal"] and connection["from"] and connection["to"] and connection["method"]:
            connections.append(connection)
        else:
            diagnostics.append({"kind": "godot_text_parse_error", "line": section.line, "message": "Signal connection is missing signal/from/to/method."})
        if len(connections) >= _MAX_CONNECTIONS:
            diagnostics.append({"kind": "godot_text_limit", "line": section.line, "message": "Signal-connection limit reached; remaining connections were skipped."})
            break

    header_uid = _decoded_attr(main_header, "uid") if main_header else ""
    sidecar_uid = read_uid_sidecar(path)
    if header_uid and sidecar_uid and header_uid != sidecar_uid:
        diagnostics.append({"kind": "godot_uid_conflict", "line": main_header.line if main_header else 1, "message": "Resource header UID differs from its .uid sidecar."})
    uid = header_uid or sidecar_uid or ""
    if uid and not uid.startswith("uid://"):
        diagnostics.append({"kind": "godot_uid_invalid", "line": main_header.line if main_header else 1, "message": "UID does not use the uid:// scheme."})
        uid = ""

    diagnostics = diagnostics[:100]
    facts = {
        "schema": 1,
        "file_kind": kind,
        "project_id": context["project_id"],
        "res_path": context["res_path"],
        "uid": uid,
        "external_resources": ext_resources,
        "node_paths": node_paths,
        "connections": connections,
        "diagnostics": diagnostics,
    }
    file_metadata = {
        "godot_kind": "file",
        "godot_project_id": context["project_id"],
        "godot_res_path": context["res_path"],
        "godot_text": facts,
    }
    if uid:
        file_metadata["godot_uid"] = uid

    nodes = [{
        "id": file_id,
        "label": path.name,
        "file_type": "code",
        "source_file": source_file,
        "source_location": "L1" if source else None,
        "metadata": sanitize_metadata(file_metadata),
    }]
    edges: list[dict] = []
    entity_by_key: dict[str, dict] = {}

    def add_entity(local_key: str, entity_id: str, label: str, entity_kind: str, line: int, metadata: dict | None = None) -> None:
        entity_metadata = {
            "godot_kind": entity_kind,
            "godot_local_key": local_key,
            "godot_project_id": context["project_id"],
            "godot_res_path": context["res_path"],
        }
        if uid:
            entity_metadata["godot_uid"] = uid
        if metadata:
            entity_metadata.update(metadata)
        node = {
            "id": entity_id,
            "label": label[:180],
            "file_type": "code",
            "source_file": source_file,
            "source_location": _source_location(line),
            "metadata": sanitize_metadata(entity_metadata),
        }
        nodes.append(node)
        entity_by_key[local_key] = node
        _add_edge(edges, file_id, entity_id, "contains", source_file, line)

    if kind == "resource":
        resource_type = _decoded_attr(main_header, "type") if main_header else ""
        script_class = _decoded_attr(main_header, "script_class") if main_header else ""
        resource_section = next((section for section in sections if section.kind == "resource"), None)
        resource_props = resource_section.properties if resource_section else []
        resource_label = _resource_name(path, resource_props)
        add_entity("resource", resource_id or file_id, resource_label, "resource", resource_section.line if resource_section else (main_header.line if main_header else 1), {
            "godot_resource_type": resource_type[:120],
            "godot_script_class": script_class[:160],
            "godot_properties": {
                prop["name"][:120]: _summarize_value(prop["value"])[0]
                for prop in resource_props[:_MAX_PROPERTIES]
            },
            "godot_property_lines": {
                prop["name"][:120]: prop["line"]
                for prop in resource_props[:_MAX_PROPERTIES]
            },
            "godot_references": [ref for ref in references if ref["owner_key"] == "resource"],
        })
    else:
        scene_label = path.stem
        scene_type = ""
        root_node_section = next((pair[0] for pair in scene_sections if pair[1] == "."), None)
        if root_node_section:
            scene_label = _decoded_attr(root_node_section, "name") or scene_label
            scene_type = _decoded_attr(root_node_section, "type")
        add_entity("scene", scene_id or file_id, scene_label, "scene", main_header.line if main_header else 1, {
            "godot_scene_type": scene_type[:120],
            "godot_references": [ref for ref in references if ref["owner_key"] == "scene"],
        })

    for sub in sub_resources:
        properties = sub["properties"]
        summary = {}
        property_lines = {}
        property_diagnostics = []
        for prop in properties[:_MAX_PROPERTIES]:
            parsed, messages = _summarize_value(prop["value"])
            summary[prop["name"][:120]] = parsed
            property_lines[prop["name"][:120]] = prop["line"]
            property_diagnostics.extend(messages)
        sub_key = f"sub:{sub['id']}"
        add_entity(sub_key, local_ids[sub_key], f"{sub['id']} ({sub['type']})".strip(), "sub_resource", sub["line"], {
            "godot_resource_type": sub["type"],
            "godot_properties": summary,
            "godot_property_lines": property_lines,
            "godot_references": [ref for ref in references if ref["owner_key"] == sub_key],
            "godot_value_diagnostics": property_diagnostics[:30],
        })

    for section, full_path, parent_path in scene_sections:
        local_key = f"node:{full_path}"
        props_summary = {}
        property_lines = {}
        value_diagnostics = []
        for prop in section.properties[:_MAX_PROPERTIES]:
            parsed, messages = _summarize_value(prop["value"])
            props_summary[prop["name"][:120]] = parsed
            property_lines[prop["name"][:120]] = prop["line"]
            value_diagnostics.extend(messages)
        node_attrs = {
            "godot_scene_path": full_path,
            "godot_node_type": _decoded_attr(section, "type")[:120],
            "godot_parent_path": parent_path[:500],
            "godot_unique_id": _decoded_attr(section, "unique_id")[:100],
            "godot_properties": props_summary,
            "godot_property_lines": property_lines,
            "godot_references": [ref for ref in references if ref["owner_key"] == local_key],
            "godot_value_diagnostics": value_diagnostics[:30],
        }
        instance_refs = _find_resource_refs(section.attributes.get("instance", ""))
        if instance_refs:
            node_attrs["godot_scene_instance"] = instance_refs[0]
        add_entity(local_key, node_ids[full_path], _decoded_attr(section, "name") or full_path, "scene_node", section.line, node_attrs)

    # Parent edges follow the authored tree. Root paths are represented by '.'.
    for section, full_path, parent_path in scene_sections:
        if full_path == ".":
            owner_id = scene_id
        else:
            normalized_parent = parent_path.strip("/") or "."
            owner_id = node_ids.get(normalized_parent, scene_id)
        child_id = node_ids.get(full_path)
        if owner_id and child_id and owner_id != child_id:
            _add_edge(edges, owner_id, child_id, "contains", source_file, section.line)

    # Resource value summaries can be useful in authored-data queries, while
    # unsupported constructors stay opaque and carry explicit diagnostics.
    for local_key, entity in entity_by_key.items():
        if local_key in {"resource", "scene"}:
            owner_section = next((section for section in sections if section.kind == "resource"), None)
            if owner_section:
                messages = []
                for prop in owner_section.properties[:_MAX_PROPERTIES]:
                    _parsed, prop_messages = _summarize_value(prop["value"])
                    messages.extend({"kind": "godot_opaque_value", "line": prop["line"], "message": message} for message in prop_messages)
                if messages:
                    entity["metadata"]["godot_value_diagnostics"] = messages[:30]

    if diagnostics:
        file_node = nodes[0]
        file_node["metadata"]["godot_text"]["diagnostics"] = diagnostics
    return {
        "nodes": nodes,
        "edges": edges,
        "godot_facts": {
            **facts,
            "references": references,
            "scene_nodes": [
                {"path": full_path, "parent": parent_path, "key": f"node:{full_path}"}
                for _section, full_path, parent_path in scene_sections
            ],
        },
        "diagnostics": [
            {
                "kind": item["kind"],
                "message": item["message"],
                "source_file": source_file,
                "source_location": f"L{item['line']}",
            }
            for item in diagnostics
        ],
    }


def extract_godot_project(path: Path, *, root: Path | None = None) -> dict:
    """Extract project identity, main-scene, autoload, and plugin settings."""
    try:
        raw = _read_source_bytes(path)
    except OSError as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}
    if len(raw) > _MAX_SOURCE_BYTES:
        return {"nodes": [], "edges": [], "error": f"Godot project settings exceed {_MAX_SOURCE_BYTES} byte limit"}

    source_file = str(path)
    context = godot_project_context(path, root)
    sections, diagnostics = _parse_sections(raw.decode("utf-8", errors="replace"), project_settings=True)
    section_map = {section.kind: section for section in sections}
    application = section_map.get("application")
    application_values = {prop["name"]: prop for prop in application.properties} if application else {}
    project_name = _decode_string(application_values.get("config/name", {}).get("value", "")) or path.parent.name
    main_scene = _decode_string(application_values.get("run/main_scene", {}).get("value", "")) or ""
    main_scene_line = int(application_values.get("run/main_scene", {}).get("line") or 1)
    file_id = _make_id(source_file)
    project_id = _make_id(file_id, "godot_project", project_name)
    project_facts: dict = {
        "schema": 1,
        "file_kind": "project",
        "project_id": context["project_id"],
        "res_path": context["res_path"],
        "main_scene": main_scene[:500],
        "main_scene_line": main_scene_line,
        "autoloads": [],
        "plugins": [],
        "diagnostics": diagnostics[:100],
    }
    project_header = section_map.get("__project_header__")
    if project_header is not None:
        version = next((prop["value"] for prop in project_header.properties if prop["name"] == "config_version"), "")
        if version:
            project_facts["config_version"] = int(version)

    nodes = [{
        "id": file_id,
        "label": path.name,
        "file_type": "code",
        "source_file": source_file,
        "source_location": "L1" if raw else None,
        "metadata": {
            "godot_kind": "file",
            "godot_project_id": context["project_id"],
            "godot_res_path": context["res_path"],
            "godot_text": project_facts,
        },
    }, {
        "id": project_id,
        "label": project_name[:180],
        "file_type": "code",
        "source_file": source_file,
        "source_location": f"L{application.line}" if application else "L1",
        "metadata": {
            "godot_kind": "godot_project",
            "godot_local_key": "project",
            "godot_project_id": context["project_id"],
            "godot_res_path": context["res_path"],
        },
    }]
    edges = [{
        "source": file_id,
        "target": project_id,
        "relation": "contains",
        "confidence": "EXTRACTED",
        "source_file": source_file,
        "source_location": _source_location(application.line if application else 1),
        "weight": 1.0,
    }]

    def add_project_entity(local_key: str, entity_id: str, label: str, kind: str, line: int, metadata: dict) -> dict:
        node = {
            "id": entity_id,
            "label": label[:180],
            "file_type": "code",
            "source_file": source_file,
            "source_location": _source_location(line),
            "metadata": {
                "godot_kind": kind,
                "godot_local_key": local_key,
                "godot_project_id": context["project_id"],
                "godot_res_path": context["res_path"],
                **metadata,
            },
        }
        nodes.append(node)
        edges.append({
            "source": project_id,
            "target": entity_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "source_file": source_file,
            "source_location": _source_location(line),
            "weight": 1.0,
        })
        return node

    if main_scene:
        add_project_entity(
            "setting:application/run/main_scene",
            _make_id(project_id, "setting", "application/run/main_scene"),
            "application/run/main_scene",
            "project_setting",
            main_scene_line,
            {"godot_setting_value": main_scene[:500]},
        )
        project_facts["main_scene_key"] = "setting:application/run/main_scene"

    autoload_section = section_map.get("autoload")
    if autoload_section:
        for prop in autoload_section.properties[:_MAX_PROPERTIES]:
            value = _decode_string(prop["value"]) or prop["value"].strip()
            singleton = value.startswith("*")
            resource_path = value[1:] if singleton else value
            if not resource_path:
                diagnostics.append({"kind": "godot_project_parse_error", "line": prop["line"], "message": f"Autoload {prop['name']!r} has an empty path."})
                continue
            local_key = f"autoload:{prop['name']}"
            add_project_entity(
                local_key,
                _make_id(project_id, "autoload", prop["name"]),
                prop["name"],
                "project_autoload",
                prop["line"],
                {"godot_autoload_path": resource_path[:500], "godot_autoload_singleton": singleton},
            )
            project_facts["autoloads"].append({
                "name": prop["name"][:160],
                "path": resource_path[:500],
                "singleton": singleton,
                "line": prop["line"],
                "key": local_key,
            })

    plugin_section = section_map.get("editor_plugins")
    enabled_plugins = next((prop for prop in plugin_section.properties if prop["name"] == "enabled"), None) if plugin_section else None
    if enabled_plugins:
        raw_value = enabled_plugins["value"]
        quoted = re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', raw_value)
        plugin_paths = [decoded for token in quoted if (decoded := _decode_string(token)) and decoded.startswith("res://")]
        for index, plugin_path in enumerate(dict.fromkeys(plugin_paths)):
            local_key = f"plugin:{index}:{plugin_path}"
            add_project_entity(
                local_key,
                _make_id(project_id, "plugin", plugin_path),
                plugin_path.rsplit("/", 1)[-1].removesuffix(".cfg"),
                "project_plugin",
                enabled_plugins["line"],
                {"godot_plugin_path": plugin_path[:500]},
            )
            project_facts["plugins"].append({"path": plugin_path[:500], "line": enabled_plugins["line"], "key": local_key})

    return {
        "nodes": nodes,
        "edges": edges,
        "godot_facts": project_facts,
        "diagnostics": [
            {
                "kind": item["kind"],
                "message": item["message"],
                "source_file": source_file,
                "source_location": f"L{item['line']}",
            }
            for item in diagnostics[:100]
        ],
    }
