"""Cross-file resolver for Godot text-resource, scene, and script links."""
from __future__ import annotations

import os
import posixpath
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Sequence


def _meta(node: dict) -> dict:
    value = node.get("metadata")
    return value if isinstance(value, dict) else {}


def _file_key(project_id: str, res_path: str) -> tuple[str, str]:
    normalized = res_path.replace("\\", "/")
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    return project_id, normalized


def _resource_path(source_res_path: str, value: str) -> str | None:
    value = value.replace("\\", "/")
    if value.startswith("res://"):
        relative = value[6:]
    elif value.startswith("uid://") or not value:
        return None
    else:
        relative = posixpath.join(posixpath.dirname(source_res_path[6:]), value)
    normalized = posixpath.normpath(relative)
    if normalized in {"", ".", ".."} or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return "res://" + normalized


def _node_target(nodes: list[dict], res_path: str) -> dict | None:
    suffix = PurePosixPath(res_path).suffix.lower()
    wanted = {
        ".gd": "script_class",
        ".tres": "resource",
        ".tscn": "scene",
    }.get(suffix)
    candidates = [node for node in nodes if _meta(node).get("godot_kind") == wanted] if wanted else []
    if len(candidates) == 1:
        return candidates[0]
    if wanted and len(candidates) > 1:
        return None
    files = [node for node in nodes if _meta(node).get("godot_kind") == "file"]
    return files[0] if len(files) == 1 else None


def _normalize_scene_path(value: str) -> str:
    value = value.replace("\\", "/").strip("/")
    if value in {"", "."}:
        return "."
    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) if parts else "."


def _resolve_scene_node_path(owner_path: str, raw_path: str) -> tuple[str | None, str]:
    """Resolve a relative NodePath from its owning scene node."""
    node_path = raw_path.split(":", 1)[0]
    if node_path.startswith("/"):
        return None, "Absolute NodePath cannot be resolved inside a scene-local graph."
    parts = [] if owner_path in {"", "."} else owner_path.split("/")
    for part in node_path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None, "Relative NodePath escapes the indexed scene root."
            parts.pop()
        else:
            parts.append(part)
    return ("/".join(parts) or "."), ""


def _append_diagnostic(file_node: dict, kind: str, message: str, line: int | None = None) -> None:
    metadata = file_node.setdefault("metadata", {})
    text = metadata.setdefault("godot_text", {})
    diagnostics = text.setdefault("diagnostics", [])
    entry = {"kind": kind, "message": message[:300]}
    if line:
        entry["line"] = int(line)
    if entry not in diagnostics and len(diagnostics) < 200:
        diagnostics.append(entry)


def _append_reference_edge(
    edges: list[dict],
    source: dict,
    target: dict,
    *,
    relation: str,
    context: str,
    line: int,
    property_name: str = "",
    reference_kind: str = "",
    raw_path: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    location = f"L{max(1, int(line or 1))}"
    metadata = {
        "godot_property": property_name[:120],
        "godot_reference_kind": reference_kind[:80],
    }
    if raw_path:
        metadata["godot_resource_path"] = raw_path[:500]
    if extra:
        metadata.update(extra)
    key = (source.get("id"), target.get("id"), relation, context, location, property_name)
    for edge in edges:
        if (
            edge.get("source"), edge.get("target"), edge.get("relation"),
            edge.get("context"), edge.get("source_location"),
            (edge.get("metadata") or {}).get("godot_property", ""),
        ) == key:
            return
    edges.append({
        "source": source["id"],
        "target": target["id"],
        "relation": relation,
        "context": context,
        "confidence": "EXTRACTED",
        "confidence_score": 1.0,
        "source_file": source.get("source_file", ""),
        "source_location": location,
        "weight": 1.0,
        "metadata": metadata,
    })


def resolve_godot_resources(
    per_file: Sequence[dict | None],
    all_nodes: list[dict],
    all_edges: list[dict],
) -> None:
    """Resolve explicit ``res://``, UID, local-resource, scene and signal links.

    File-node metadata carries the portable text facts. This lets incremental
    builds resolve links from unchanged Godot files without reparsing or storing
    filesystem-absolute paths in the graph.
    """
    del per_file  # all resolution facts are carried on the indexed nodes
    nodes_by_id = {str(node.get("id")): node for node in all_nodes if node.get("id")}
    files: list[dict] = []
    entities_by_file: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    nodes_by_resource_path: dict[tuple[str, str], list[dict]] = defaultdict(list)
    uid_to_resource_keys: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    methods_by_class: dict[str, list[dict]] = defaultdict(list)

    for node in all_nodes:
        metadata = _meta(node)
        kind = metadata.get("godot_kind")
        project_id = str(metadata.get("godot_project_id") or ".")
        res_path = str(metadata.get("godot_res_path") or "")
        resource_key = _file_key(project_id, res_path) if res_path else None
        if resource_key:
            nodes_by_resource_path[resource_key].append(node)
            local_key = metadata.get("godot_local_key")
            if local_key:
                entities_by_file[resource_key][str(local_key)] = node
            uid = str(metadata.get("godot_uid") or "")
            if uid:
                uid_to_resource_keys[(project_id, uid)].add(resource_key)
        text_facts = metadata.get("godot_text")
        if kind == "file" and isinstance(text_facts, dict):
            files.append({
                "node": node,
                "key": resource_key,
                "project_id": project_id,
                "res_path": res_path,
                "facts": text_facts,
            })
        if kind == "script_class":
            continue

    for edge in all_edges:
        if edge.get("relation") == "method":
            methods_by_class[str(edge.get("source"))].append(nodes_by_id.get(str(edge.get("target")), {}))

    files_by_key = {file["key"]: file for file in files if file["key"]}
    attached_scripts: dict[tuple[tuple[str, str], str], dict] = {}
    unresolved_ext_ids: dict[tuple[str, str], set[str]] = {}

    def resolve_path(file: dict, path: str, uid: str = "") -> tuple[dict | None, str, str]:
        """Return (target node, canonical res path, diagnostic reason)."""
        project_id = file["project_id"]
        candidates_by_path: list[dict] = []
        canonical_path = ""
        if path.startswith("uid://") and not uid:
            uid = path
            path = ""
        if path:
            canonical_path = _resource_path(file["res_path"], path) or ""
            if canonical_path:
                candidates_by_path = nodes_by_resource_path.get(_file_key(project_id, canonical_path), [])
        uid_keys = uid_to_resource_keys.get((project_id, uid), set()) if uid else set()
        uid_key = next(iter(uid_keys)) if len(uid_keys) == 1 else None
        if uid and len(uid_keys) > 1:
            return None, canonical_path, f"UID {uid} matches multiple project files."
        if candidates_by_path:
            path_key = _file_key(project_id, canonical_path)
            if uid_key and uid_key != path_key:
                return None, canonical_path, f"Path {canonical_path} and UID {uid} identify different files."
            target = _node_target(candidates_by_path, canonical_path)
            return target, canonical_path, "" if target else f"Resource path {canonical_path} has ambiguous graph targets."
        if uid_key:
            indexed = nodes_by_resource_path.get(uid_key, [])
            target = _node_target(indexed, uid_key[1])
            return target, uid_key[1], "" if target else f"UID {uid} resolved to a file with ambiguous graph targets."
        if canonical_path:
            return None, canonical_path, f"Resource path {canonical_path} is outside the indexed graph."
        if uid:
            return None, "", f"UID {uid} is not indexed in this Godot project."
        return None, "", "External resource has neither a resolvable path nor UID."

    def report(file: dict, message: str, line: int | None = None) -> None:
        _append_diagnostic(file["node"], "godot_unresolved_reference", message, line)

    for file in files:
        key = file["key"]
        if not key:
            continue
        file_node = file["node"]
        facts = file["facts"]
        ext_rows = facts.get("external_resources") if isinstance(facts.get("external_resources"), list) else []
        ext_by_id: dict[str, dict] = {}
        duplicate_ids: set[str] = set()
        for ext in ext_rows:
            if not isinstance(ext, dict):
                continue
            local_id = str(ext.get("id") or "")
            if not local_id:
                continue
            if local_id in ext_by_id:
                duplicate_ids.add(local_id)
            ext_by_id[local_id] = ext
        local_entities = entities_by_file.get(key, {})
        used_ext_ids: set[str] = set()

        for owner in nodes_by_resource_path.get(key, []):
            owner_metadata = _meta(owner)
            references = owner_metadata.get("godot_references")
            if not isinstance(references, list):
                continue
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                target_kind = str(reference.get("target_kind") or "")
                target_id = str(reference.get("target_id") or "")
                target_path = str(reference.get("target_path") or "")
                property_name = str(reference.get("property") or "")
                line = int(reference.get("line") or 1)
                target: dict | None = None
                canonical_path = ""
                reason = ""
                if target_kind == "sub_resource":
                    target = local_entities.get(f"sub:{target_id}")
                    if target is None:
                        reason = f"SubResource id {target_id!r} is not defined in this file."
                elif target_kind == "ext_resource":
                    if target_id in duplicate_ids:
                        reason = f"External resource id {target_id!r} is duplicated in this file."
                    else:
                        ext = ext_by_id.get(target_id)
                        if ext:
                            target, canonical_path, reason = resolve_path(file, str(ext.get("path") or ""), str(ext.get("uid") or ""))
                            used_ext_ids.add(target_id)
                        else:
                            reason = f"ExtResource id {target_id!r} is not defined in this file."
                elif target_kind == "path":
                    target, canonical_path, reason = resolve_path(file, target_path)
                else:
                    reason = f"Unsupported Godot resource reference kind {target_kind!r}."
                if target is None:
                    report(file, reason, line)
                    continue

                is_instance = property_name == "instance" and owner_metadata.get("godot_kind") == "scene_node"
                is_script = property_name == "script"
                relation = "uses" if is_instance else "references"
                context = "godot_scene_instance" if is_instance else "godot_script_attachment" if is_script else "godot_resource_property"
                _append_reference_edge(
                    all_edges,
                    owner,
                    target,
                    relation=relation,
                    context=context,
                    line=line,
                    property_name=property_name,
                    reference_kind=target_kind,
                    raw_path=canonical_path or target_path,
                    extra={"godot_local_id": target_id} if target_id else None,
                )
                if is_script and owner_metadata.get("godot_kind") == "scene_node" and _meta(target).get("godot_kind") == "script_class":
                    attached_scripts[(key, str(owner_metadata.get("godot_scene_path") or "."))] = target

        # Preserve declared but unused external-resource dependencies too. Their
        # owner is the file hub because no property/node provides a narrower fact.
        for ext in ext_rows:
            if not isinstance(ext, dict):
                continue
            local_id = str(ext.get("id") or "")
            if not local_id or local_id in used_ext_ids or local_id in duplicate_ids:
                continue
            target, canonical_path, reason = resolve_path(file, str(ext.get("path") or ""), str(ext.get("uid") or ""))
            if target is None:
                report(file, reason, int(ext.get("line") or 1))
                continue
            _append_reference_edge(
                all_edges,
                file_node,
                target,
                relation="references",
                context="godot_external_resource",
                line=int(ext.get("line") or 1),
                property_name="",
                reference_kind="ext_resource",
                raw_path=canonical_path,
                extra={"godot_local_id": local_id},
            )

    # NodePath properties resolve from the node that owns the property. Paths
    # into inherited or instanced scene interiors stay unresolved because those
    # nodes are not flattened into this scene's authored graph.
    for file in files:
        key = file["key"]
        if not key or file["facts"].get("file_kind") != "scene":
            continue
        local_entities = entities_by_file.get(key, {})
        scene_nodes = {
            str(_meta(node).get("godot_scene_path")): node
            for node in local_entities.values()
            if _meta(node).get("godot_kind") == "scene_node"
        }
        for node_path in file["facts"].get("node_paths", []):
            if not isinstance(node_path, dict):
                continue
            owner = local_entities.get(str(node_path.get("owner_key") or ""))
            raw_path = str(node_path.get("path") or "")
            property_name = str(node_path.get("property") or "")
            line = int(node_path.get("line") or 1)
            if owner is None:
                report(file, "NodePath property owner is not present in the indexed scene.", line)
                continue
            owner_path = str(_meta(owner).get("godot_scene_path") or ".")
            target_path, reason = _resolve_scene_node_path(owner_path, raw_path)
            if reason:
                report(file, f"NodePath {raw_path!r} cannot be resolved: {reason}", line)
                continue
            target = scene_nodes.get(target_path or "")
            if target is None:
                report(file, f"NodePath {raw_path!r} resolves to unindexed scene node {target_path!r}.", line)
                continue
            if owner.get("id") == target.get("id"):
                continue
            _append_reference_edge(
                all_edges,
                owner,
                target,
                relation="references",
                context="godot_node_path",
                line=line,
                property_name=property_name,
                reference_kind="node_path",
                extra={
                    "godot_node_path": raw_path[:500],
                    "godot_target_node_path": (target_path or "")[:500],
                },
            )

    # A serialized signal connection is a static wiring fact. Resolve a callback
    # to a method only when the destination node has an explicit script attachment.
    for file in files:
        key = file["key"]
        if not key or file["facts"].get("file_kind") != "scene":
            continue
        local_entities = entities_by_file.get(key, {})
        scene_nodes = {
            str(_meta(node).get("godot_scene_path")): node
            for node in local_entities.values()
            if _meta(node).get("godot_kind") == "scene_node"
        }
        for connection in file["facts"].get("connections", []):
            if not isinstance(connection, dict):
                continue
            source_path = _normalize_scene_path(str(connection.get("from") or "."))
            target_path = _normalize_scene_path(str(connection.get("to") or "."))
            source_node = scene_nodes.get(source_path)
            target_node = scene_nodes.get(target_path)
            line = int(connection.get("line") or 1)
            signal_name = str(connection.get("signal") or "")
            method_name = str(connection.get("method") or "")
            if source_node is None or target_node is None:
                report(file, f"Signal {signal_name!r} references an unindexed scene node ({source_path!r} -> {target_path!r}).", line)
                continue
            script_node = attached_scripts.get((key, target_path))
            method_node = None
            if script_node:
                candidates = [
                    node for node in methods_by_class.get(str(script_node.get("id")), [])
                    if str(node.get("label") or "").strip().lstrip(".").removesuffix("()") == method_name
                ]
                if len(candidates) == 1 and candidates[0].get("id"):
                    method_node = candidates[0]
                elif len(candidates) > 1:
                    report(file, f"Signal callback {method_name!r} matches multiple methods on the attached script.", line)
                else:
                    report(file, f"Signal callback {method_name!r} is not declared by the attached script.", line)
            elif method_name:
                report(file, f"Signal callback {method_name!r} has no explicit script attachment on node {target_path!r}.", line)
            endpoint = method_node or target_node
            _append_reference_edge(
                all_edges,
                source_node,
                endpoint,
                relation="references",
                context="godot_signal_connection",
                line=line,
                property_name=signal_name,
                reference_kind="signal_connection",
                extra={
                    "godot_signal": signal_name[:160],
                    "godot_target_node_path": target_path[:500],
                    "godot_callback_method": method_name[:160],
                    "godot_callback_resolved": method_node is not None,
                    "godot_callback_node_id": target_node.get("id", ""),
                },
            )
