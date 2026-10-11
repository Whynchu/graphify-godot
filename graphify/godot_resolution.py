"""Cross-file resolver for Godot text-resource, scene, and script links."""
from __future__ import annotations

import os
import posixpath
import re
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


def _source_path_key(value: object) -> str:
    normalized = posixpath.normpath(str(value or "").replace("\\", "/"))
    if os.name == "nt":
        normalized = normalized.casefold()
    return normalized


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


def _resolve_resource_path(
    file: dict,
    path: str,
    uid: str,
    nodes_by_resource_path: dict[tuple[str, str], list[dict]],
    uid_to_resource_keys: dict[tuple[str, str], set[tuple[str, str]]],
) -> tuple[dict | None, str, str]:
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


def _append_script_diagnostic(file_node: dict, message: str, line: int | None = None) -> None:
    metadata = file_node.setdefault("metadata", {})
    script = metadata.setdefault("godot_script", {})
    diagnostics = script.setdefault("resolution_diagnostics", [])
    entry = {"kind": "godot_unresolved_reference", "message": message[:300]}
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
    script_files: list[dict] = []
    verification_manifests: list[dict] = []
    global_classes_by_project: dict[tuple[str, str], list[dict]] = defaultdict(list)
    classes_by_project: dict[tuple[str, str], list[dict]] = defaultdict(list)
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
            if not local_key and kind == "godot_project":
                local_key = "project"
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
        script_facts = metadata.get("godot_script")
        if kind == "file" and isinstance(script_facts, dict):
            script_files.append({
                "node": node,
                "key": resource_key,
                "project_id": project_id,
                "res_path": res_path,
                "facts": script_facts,
            })
        manifest_facts = metadata.get("verification_manifest")
        if isinstance(manifest_facts, dict):
            verification_manifests.append({"node": node, "facts": manifest_facts})
        if kind == "script_class":
            classes_by_project[(project_id, str(node.get("label") or ""))].append(node)
            if metadata.get("godot_global_class"):
                global_classes_by_project[(project_id, str(node.get("label") or ""))].append(node)
        elif kind == "script_inner_class":
            classes_by_project[(project_id, str(node.get("label") or ""))].append(node)

    for edge in all_edges:
        if edge.get("relation") == "method":
            methods_by_class[str(edge.get("source"))].append(nodes_by_id.get(str(edge.get("target")), {}))

    files_by_key = {file["key"]: file for file in files if file["key"]}
    attached_scripts: dict[tuple[tuple[str, str], str], dict] = {}
    resource_script_attachments: dict[str, dict] = {}
    autoload_scripts_by_name: dict[tuple[str, str], list[dict]] = defaultdict(list)
    preload_aliases_by_scope: dict[tuple[tuple[str, str], str, str], list[dict]] = defaultdict(list)
    inherited_scene_keys: dict[tuple[str, str], tuple[str, str]] = {}
    unresolved_ext_ids: dict[tuple[str, str], set[str]] = {}

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
                            target, canonical_path, reason = _resolve_resource_path(
                                file,
                                str(ext.get("path") or ""),
                                str(ext.get("uid") or ""),
                                nodes_by_resource_path,
                                uid_to_resource_keys,
                            )
                            used_ext_ids.add(target_id)
                        else:
                            reason = f"ExtResource id {target_id!r} is not defined in this file."
                elif target_kind == "path":
                    target, canonical_path, reason = _resolve_resource_path(
                        file, target_path, "", nodes_by_resource_path, uid_to_resource_keys
                    )
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
                elif is_script and owner_metadata.get("godot_kind") in {"resource", "sub_resource"} and _meta(target).get("godot_kind") == "script_class":
                    resource_script_attachments[str(owner.get("id") or "")] = target
                if (
                    is_instance
                    and str(owner_metadata.get("godot_scene_path") or ".") == "."
                    and _meta(target).get("godot_kind") == "scene"
                ):
                    base_key = _file_key(str(_meta(target).get("godot_project_id") or file["project_id"]), str(_meta(target).get("godot_res_path") or ""))
                    if base_key and base_key != key:
                        inherited_scene_keys[key] = base_key

        # Preserve declared but unused external-resource dependencies too. Their
        # owner is the file hub because no property/node provides a narrower fact.
        for ext in ext_rows:
            if not isinstance(ext, dict):
                continue
            local_id = str(ext.get("id") or "")
            if not local_id or local_id in used_ext_ids or local_id in duplicate_ids:
                continue
            target, canonical_path, reason = _resolve_resource_path(
                file,
                str(ext.get("path") or ""),
                str(ext.get("uid") or ""),
                nodes_by_resource_path,
                uid_to_resource_keys,
            )
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

    def scene_lineage(scene_key: tuple[str, str]) -> list[tuple[str, str]]:
        lineage: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        current = scene_key
        while current:
            if current in seen:
                lineage.append(current)
                break
            seen.add(current)
            lineage.append(current)
            current = inherited_scene_keys.get(current)  # type: ignore[assignment]
        return lineage

    def scene_nodes_for(scene_key: tuple[str, str]) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for ancestor_key in reversed(scene_lineage(scene_key)):
            for node in entities_by_file.get(ancestor_key, {}).values():
                if _meta(node).get("godot_kind") != "scene_node":
                    continue
                path = str(_meta(node).get("godot_scene_path") or ".")
                merged[path] = node
        return merged

    def attached_script_for(scene_key: tuple[str, str], scene_path: str) -> dict | None:
        for ancestor_key in scene_lineage(scene_key):
            script = attached_scripts.get((ancestor_key, scene_path))
            if script is not None:
                return script
        return None

    for scene_key in inherited_scene_keys:
        lineage = scene_lineage(scene_key)
        if len(lineage) != len(set(lineage)):
            report(files_by_key[scene_key], "Inherited scene chain contains a cycle.")

    # Project settings are resolved after scene parsing so an autoload that
    # points at a scene can inherit the root node's explicit script attachment.
    for file in files:
        facts = file["facts"]
        if facts.get("file_kind") != "project" or not file["key"]:
            continue
        local_entities = entities_by_file.get(file["key"], {})
        project_node = local_entities.get("project")
        if project_node is None:
            continue
        main_scene = str(facts.get("main_scene") or "")
        if main_scene:
            setting_node = local_entities.get(str(facts.get("main_scene_key") or "")) or project_node
            target, canonical_path, reason = _resolve_resource_path(
                file, main_scene, "", nodes_by_resource_path, uid_to_resource_keys
            )
            if target is None:
                report(file, f"Main scene {main_scene!r} cannot be resolved: {reason}", int(facts.get("main_scene_line") or 1))
            else:
                _append_reference_edge(
                    all_edges,
                    setting_node,
                    target,
                    relation="uses",
                    context="godot_project_main_scene",
                    line=int(facts.get("main_scene_line") or 1),
                    property_name="run/main_scene",
                    reference_kind="main_scene",
                    raw_path=canonical_path or main_scene,
                )

        for autoload in facts.get("autoloads", []):
            if not isinstance(autoload, dict):
                continue
            name = str(autoload.get("name") or "")
            path = str(autoload.get("path") or "")
            autoload_node = local_entities.get(str(autoload.get("key") or ""))
            if not autoload_node or not name or not path:
                continue
            target, canonical_path, reason = _resolve_resource_path(
                file, path, "", nodes_by_resource_path, uid_to_resource_keys
            )
            if target is None:
                report(file, f"Autoload {name!r} cannot be resolved: {reason}", int(autoload.get("line") or 1))
                continue
            runtime_script = target if _meta(target).get("godot_kind") == "script_class" else None
            if _meta(target).get("godot_kind") == "scene":
                target_key = _file_key(str(_meta(target).get("godot_project_id") or file["project_id"]), str(_meta(target).get("godot_res_path") or ""))
                runtime_script = attached_script_for(target_key, ".") if target_key else None
            _append_reference_edge(
                all_edges,
                autoload_node,
                runtime_script or target,
                relation="uses",
                context="godot_project_autoload",
                line=int(autoload.get("line") or 1),
                property_name=name,
                reference_kind="autoload",
                raw_path=canonical_path or path,
                extra={"godot_autoload_singleton": bool(autoload.get("singleton"))},
            )
            if runtime_script is not None:
                autoload_scripts_by_name[(file["project_id"], name)].append(runtime_script)

        for plugin in facts.get("plugins", []):
            if not isinstance(plugin, dict):
                continue
            path = str(plugin.get("path") or "")
            plugin_node = local_entities.get(str(plugin.get("key") or ""))
            if not plugin_node or not path:
                continue
            target, canonical_path, reason = _resolve_resource_path(
                file, path, "", nodes_by_resource_path, uid_to_resource_keys
            )
            if target is None:
                report(file, f"Enabled editor plugin {path!r} cannot be resolved: {reason}", int(plugin.get("line") or 1))
                continue
            _append_reference_edge(
                all_edges,
                plugin_node,
                target,
                relation="uses",
                context="godot_project_plugin",
                line=int(plugin.get("line") or 1),
                property_name="editor_plugins/enabled",
                reference_kind="enabled_plugin",
                raw_path=canonical_path or path,
            )

    # GDScript dependencies use the same project-local path and UID index as
    # authored .tres/.tscn references. Emit only literal source-resource loads
    # and identifiers that match one unique project class; never resolve a name
    # against a same-named method or an unrelated project.
    for file in script_files:
        key = file["key"]
        if not key:
            continue
        source_candidates = nodes_by_resource_path.get(key, [])
        source_script = _node_target(source_candidates, file["res_path"])
        if source_script is None:
            continue
        facts = file["facts"]
        for reference in facts.get("resource_references", []):
            if not isinstance(reference, dict):
                continue
            kind = str(reference.get("kind") or "load")
            path = str(reference.get("path") or "")
            line = int(reference.get("line") or 1)
            target, canonical_path, reason = _resolve_resource_path(
                file, path, "", nodes_by_resource_path, uid_to_resource_keys
            )
            if target is None:
                _append_script_diagnostic(file["node"], reason, line)
                continue
            if target.get("id") == source_script.get("id"):
                continue
            _append_reference_edge(
                all_edges,
                source_script,
                target,
                relation="uses",
                context=f"godot_script_{kind}",
                line=line,
                reference_kind=kind,
                raw_path=canonical_path or path,
                extra={"godot_script_binding": str(reference.get("binding") or "")},
            )
            binding = str(reference.get("binding") or "")
            if kind == "preload" and binding:
                alias_script = target if _meta(target).get("godot_kind") == "script_class" else resource_script_attachments.get(str(target.get("id") or ""))
                if alias_script is not None:
                    owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
                    preload_aliases_by_scope[(key, owner_line, binding)].append({
                        "script": alias_script,
                        "instance_resource": _meta(target).get("godot_kind") != "script_class",
                    })

        for reference in facts.get("class_references", []):
            if not isinstance(reference, dict):
                continue
            class_name = str(reference.get("name") or "")
            ref_kind = str(reference.get("kind") or "class_receiver")
            line = int(reference.get("line") or 1)
            candidates = global_classes_by_project.get((file["project_id"], class_name), [])
            if len(candidates) > 1:
                _append_script_diagnostic(
                    file["node"],
                    f"Global class name {class_name!r} matches multiple scripts in this project.",
                    line,
                )
                continue
            if not candidates or candidates[0].get("id") == source_script.get("id"):
                continue
            relation = "inherits" if ref_kind == "extends" else "references"
            context = "godot_script_extends" if ref_kind == "extends" else "godot_global_class_reference"
            _append_reference_edge(
                all_edges,
                source_script,
                candidates[0],
                relation=relation,
                context=context,
                line=line,
                property_name=ref_kind,
                reference_kind="global_class",
                extra={"godot_global_class": class_name},
            )

    def method_named(class_id: str, method_name: str) -> list[dict]:
        return [
            method for method in methods_by_class.get(class_id, [])
            if str(method.get("label") or "").strip().lstrip(".").removesuffix("()") == method_name
        ]

    def find_method(class_id: str, method_name: str, *, skip_class: bool = False) -> dict | None:
        seen: set[str] = set()
        current = class_id
        first = True
        while current and current not in seen:
            seen.add(current)
            candidates = [] if first and skip_class else method_named(current, method_name)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                return None
            bases = base_classes.get(current, [])
            if len(bases) != 1:
                return None
            current = bases[0]
            first = False
        return None

    base_classes: dict[str, list[str]] = defaultdict(list)
    for edge in all_edges:
        if edge.get("relation") == "inherits" and edge.get("source") and edge.get("target"):
            base_classes[str(edge["source"])].append(str(edge["target"]))

    # A script's NodePath receiver is meaningful only for scene nodes where that
    # exact script is attached. Keep candidates project-local and require one
    # script target before promoting later `node_var.method()` calls.
    node_receiver_script_targets: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for file in script_files:
        if not file["key"]:
            continue
        source_script = _node_target(nodes_by_resource_path.get(file["key"], []), file["res_path"])
        if source_script is None:
            continue
        attached_rows = [
            (scene_key, scene_path)
            for (scene_key, scene_path), script in attached_scripts.items()
            if str(script.get("id") or "") == str(source_script.get("id") or "")
        ]
        for reference in file["facts"].get("node_references", []):
            if not isinstance(reference, dict):
                continue
            line = int(reference.get("line") or 1)
            if reference.get("dynamic"):
                _append_script_diagnostic(file["node"], "Dynamic get_node path remains unresolved; only literal scene NodePaths are indexed.", line)
                continue
            raw_path = str(reference.get("path") or "")
            syntax = str(reference.get("syntax") or "")
            if not raw_path:
                continue
            owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
            owner_class = source_script
            if owner_line != str(source_script.get("source_location") or ""):
                owner_candidates = [
                    node for node in nodes_by_resource_path.get(file["key"], [])
                    if _meta(node).get("godot_kind") == "script_inner_class"
                    and str(node.get("source_location") or "") == owner_line
                ]
                if len(owner_candidates) != 1:
                    continue
                owner_class = owner_candidates[0]
            caller_line = f"L{max(1, int(reference.get('caller_line') or 1))}"
            caller_name = str(reference.get("caller_name") or "")
            caller_candidates = [
                method for method in method_named(str(owner_class.get("id") or ""), caller_name)
                if str(method.get("source_location") or "") == caller_line
            ]
            caller = caller_candidates[0] if len(caller_candidates) == 1 else owner_class
            for scene_key, owner_path in attached_rows:
                scene_nodes = scene_nodes_for(scene_key).values()
                if syntax == "unique":
                    targets = [
                        node for node in scene_nodes
                        if str(node.get("label") or "") == raw_path
                        and (_meta(node).get("godot_properties") or {}).get("unique_name_in_owner") is True
                    ]
                    if len(targets) != 1:
                        _append_script_diagnostic(file["node"], f"Unique scene node %{raw_path} is not unique in attached scene {scene_key[1]}.", line)
                        continue
                    target = targets[0]
                    target_path = str(_meta(target).get("godot_scene_path") or ".")
                else:
                    target_path, reason = _resolve_scene_node_path(owner_path, raw_path)
                    if reason:
                        _append_script_diagnostic(file["node"], f"NodePath {raw_path!r} cannot be resolved: {reason}", line)
                        continue
                    target = next((node for node in scene_nodes if str(_meta(node).get("godot_scene_path") or "") == target_path), None)
                    if target is None:
                        _append_script_diagnostic(file["node"], f"NodePath {raw_path!r} resolves to unindexed scene node {target_path!r}.", line)
                        continue
                _append_reference_edge(
                    all_edges,
                    caller,
                    target,
                    relation="references",
                    context="godot_script_unique_node" if syntax == "unique" else "godot_script_node_path",
                    line=line,
                    property_name=str(reference.get("binding") or ""),
                    reference_kind=syntax,
                    raw_path=raw_path,
                    extra={"godot_target_node_path": target_path[:500]},
                )
                binding_name = str(reference.get("binding") or "")
                target_script = attached_script_for(scene_key, target_path)
                if binding_name and target_script is not None:
                    node_receiver_script_targets[(file["key"][1], owner_line, caller_line, binding_name)].add(str(target_script.get("id") or ""))
                callee_name = str(reference.get("callee") or "")
                if callee_name and target_script is not None:
                    target_method = find_method(str(target_script.get("id") or ""), callee_name)
                    if target_method is not None and caller.get("id") != target_method.get("id"):
                        _append_reference_edge(
                            all_edges,
                            caller,
                            target_method,
                            relation="calls",
                            context="godot_scene_attached_call",
                            line=line,
                            property_name=callee_name,
                            reference_kind="scene_attached_method",
                            raw_path=raw_path,
                        )

    resolved_dynamic_calls: set[tuple[tuple[str, str], str, str, int, str]] = set()
    existing_call_facts = {
        (str(edge.get("source")), str(edge.get("target")), str(edge.get("source_location")))
        for edge in all_edges if edge.get("relation") == "calls"
    }
    for file in script_files:
        facts = file["facts"]
        if not isinstance(facts.get("call_references"), list):
            continue
        script_node = _node_target(nodes_by_resource_path.get(file["key"], []), file["res_path"])
        if script_node is None:
            continue
        for reference in facts["call_references"]:
            if not isinstance(reference, dict) or reference.get("shadowed"):
                continue
            caller_name = str(reference.get("caller_name") or "")
            caller_line = f"L{max(1, int(reference.get('caller_line') or 1))}"
            owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
            owner_id = str(script_node.get("id") or "")
            if owner_line != str(script_node.get("source_location") or ""):
                owner_candidates = [
                    node for node in nodes_by_resource_path.get(file["key"], [])
                    if _meta(node).get("godot_kind") == "script_inner_class"
                    and str(node.get("source_location") or "") == owner_line
                ]
                if len(owner_candidates) != 1:
                    continue
                owner_id = str(owner_candidates[0].get("id") or "")
            caller_candidates = [method for method in method_named(owner_id, caller_name)
                                 if str(method.get("source_location") or "") == caller_line]
            caller_id = str(caller_candidates[0].get("id") or "") if len(caller_candidates) == 1 else ""
            method_name = str(reference.get("callee") or "")
            receiver_kind = str(reference.get("receiver_kind") or "unqualified")
            inferred_receiver = False
            autoload_receiver = False
            preload_receiver_instance = False
            scene_attached_receiver = False
            if not caller_id or not method_name:
                continue
            target_classes: list[str] = []
            if receiver_kind in {"unqualified", "self"}:
                if owner_id:
                    target_classes = [owner_id]
            elif receiver_kind == "super" and owner_id:
                bases = base_classes.get(owner_id, [])
                if len(bases) == 1:
                    target_classes = [bases[0]]
            elif receiver_kind == "class":
                receiver_name = str(reference.get("receiver") or "")
                matches = global_classes_by_project.get((file["project_id"], receiver_name), [])
                if len(matches) == 1:
                    target_classes = [str(matches[0].get("id") or "")]
                elif len(matches) > 1:
                    _append_script_diagnostic(file["node"], f"Static receiver {receiver_name!r} matches multiple project classes.", int(reference.get("line") or 1))
                else:
                    autoload_matches = autoload_scripts_by_name.get((file["project_id"], receiver_name), [])
                    if len(autoload_matches) == 1:
                        target_classes = [str(autoload_matches[0].get("id") or "")]
                        autoload_receiver = True
                    elif len(autoload_matches) > 1:
                        _append_script_diagnostic(file["node"], f"Autoload receiver {receiver_name!r} is ambiguous in this project.", int(reference.get("line") or 1))
                    else:
                        owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
                        aliases = preload_aliases_by_scope.get((file["key"], owner_line, receiver_name), [])
                        if len(aliases) == 1:
                            target_classes = [str(aliases[0]["script"].get("id") or "")]
                            preload_receiver_instance = bool(aliases[0].get("instance_resource"))
                        elif len(aliases) > 1:
                            _append_script_diagnostic(file["node"], f"Preload alias {receiver_name!r} is ambiguous in this script scope.", int(reference.get("line") or 1))
            elif receiver_kind == "identifier":
                receiver_name = str(reference.get("receiver") or "")
                binding_matches = [
                    binding for binding in facts.get("receiver_bindings", [])
                    if isinstance(binding, dict)
                    and str(binding.get("name") or "") == receiver_name
                    and str(binding.get("owner_class_line") or "") == str(reference.get("owner_class_line") or "")
                    and (not binding.get("caller_line") or str(binding.get("caller_line")) == str(reference.get("caller_line")))
                ]
                if not binding_matches:
                    binding_matches = [
                        binding for binding in facts.get("receiver_bindings", [])
                        if isinstance(binding, dict)
                        and str(binding.get("name") or "") == receiver_name
                        and str(binding.get("owner_class_line") or "") == str(reference.get("owner_class_line") or "")
                        and not binding.get("caller_line")
                    ]
                type_names = {str(binding.get("type") or "").strip() for binding in binding_matches}
                if len(type_names) == 1:
                    type_name = next(iter(type_names))
                    if reference.get("receiver_indexed"):
                        array_type = re.fullmatch(r"(?:Array|TypedArray)\s*\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]", type_name)
                        dictionary_type = re.fullmatch(r"Dictionary\s*\[\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]", type_name)
                        type_name = (array_type or dictionary_type).group(1) if array_type or dictionary_type else ""
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", type_name):
                        type_candidates = classes_by_project.get((file["project_id"], type_name), [])
                        if len(type_candidates) == 1:
                            target_classes = [str(type_candidates[0].get("id") or "")]
                            inferred_receiver = any(bool(binding.get("inferred")) for binding in binding_matches)
                        else:
                            owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
                            aliases = preload_aliases_by_scope.get((file["key"], owner_line, type_name), [])
                            if len(aliases) == 1:
                                target_classes = [str(aliases[0]["script"].get("id") or "")]
                                inferred_receiver = any(bool(binding.get("inferred")) for binding in binding_matches)
                            elif len(aliases) > 1:
                                _append_script_diagnostic(file["node"], f"Preload alias type {type_name!r} is ambiguous in this script scope.", int(reference.get("line") or 1))
                if not target_classes:
                    scene_targets = node_receiver_script_targets.get((file["key"][1], owner_line, caller_line, receiver_name), set())
                    if len(scene_targets) == 1:
                        target_classes = list(scene_targets)
                        scene_attached_receiver = True
                    elif len(scene_targets) > 1:
                        _append_script_diagnostic(file["node"], f"Scene node receiver {receiver_name!r} resolves to multiple attached scripts.", int(reference.get("line") or 1))
                if not target_classes:
                    autoload_matches = autoload_scripts_by_name.get((file["project_id"], receiver_name), [])
                    if len(autoload_matches) == 1:
                        target_classes = [str(autoload_matches[0].get("id") or "")]
                        autoload_receiver = True
                    elif len(autoload_matches) > 1:
                        _append_script_diagnostic(file["node"], f"Autoload receiver {receiver_name!r} is ambiguous in this project.", int(reference.get("line") or 1))
            if not target_classes:
                continue
            if receiver_kind == "super":
                candidates = [find_method(class_id, method_name) for class_id in target_classes]
                candidates = [candidate for candidate in candidates if candidate]
            else:
                candidates = [find_method(class_id, method_name) for class_id in target_classes]
                candidates = [candidate for candidate in candidates if candidate]
            if len(candidates) != 1:
                continue
            target = candidates[0]
            target_id = str(target.get("id") or "")
            if not target_id or target_id == caller_id:
                continue
            if receiver_kind == "class" and not autoload_receiver and not preload_receiver_instance and not (_meta(target).get("godot_static")):
                continue
            location = f"L{max(1, int(reference.get('line') or 1))}"
            if reference.get("dynamic_dispatch"):
                resolved_dynamic_calls.add((
                    file["key"],
                    owner_line,
                    caller_line,
                    int(reference.get("line") or 1),
                    str(reference.get("dispatch_operation") or "call"),
                ))
            pair = (caller_id, target_id, location)
            if pair in existing_call_facts:
                continue
            existing_call_facts.add(pair)
            confidence = "INFERRED" if inferred_receiver else "EXTRACTED"
            all_edges.append({
                "source": caller_id,
                "target": target_id,
                "relation": "calls",
                "context": "godot_scene_attached_call" if scene_attached_receiver else "godot_dynamic_literal_call" if reference.get("dynamic_dispatch") else "godot_gdscript_call",
                "confidence": confidence,
                "confidence_score": 0.85 if inferred_receiver else 1.0,
                "source_file": file["node"].get("source_file", ""),
                "source_location": location,
                "weight": 1.0,
                **({"godot_dynamic_operation": str(reference.get("dispatch_operation") or "")[:80]} if reference.get("dynamic_dispatch") else {}),
            })

    for file in script_files:
        facts = file["facts"]
        for reference in facts.get("dynamic_references", []):
            if not isinstance(reference, dict):
                continue
            kind = str(reference.get("kind") or "")
            operation = str(reference.get("operation") or "")
            line = int(reference.get("line") or 1)
            if kind == "computed_resource_load":
                _append_script_diagnostic(file["node"], f"Computed {operation} path remains unresolved; only literal project resource paths are indexed.", line)
            elif kind == "dynamic_dispatch":
                method = str(reference.get("literal_method") or "")
                key = (
                    file["key"],
                    f"L{max(1, int(reference.get('owner_class_line') or 1))}",
                    f"L{max(1, int(reference.get('caller_line') or 1))}",
                    line,
                    operation,
                )
                if method and key in resolved_dynamic_calls:
                    continue
                detail = f"literal target {method!r} lacks one resolved script receiver" if method else "the target method is computed at runtime"
                _append_script_diagnostic(file["node"], f"Dynamic {operation} remains unresolved because {detail}.", line)
            elif kind == "runtime_group_dispatch":
                group = str(reference.get("literal_group") or "")
                detail = f" for group {group!r}" if group else ""
                _append_script_diagnostic(file["node"], f"{operation}{detail} depends on runtime group membership; recipients are not statically resolved.", line)
            elif kind == "runtime_group_lookup":
                group = str(reference.get("literal_group") or "")
                detail = f" for group {group!r}" if group else ""
                _append_script_diagnostic(file["node"], f"{operation}{detail} is a runtime group lookup; node membership is not statically resolved.", line)
            elif kind == "runtime_script_assignment":
                _append_script_diagnostic(file["node"], "Runtime set_script() assignment can change an object's script outside serialized scene/resource attachments.", line)
            elif kind == "runtime_node_injection":
                _append_script_diagnostic(file["node"], "Runtime add_child() injection can add nodes not present in indexed scene resources.", line)

    property_nodes_by_owner: dict[str, list[dict]] = defaultdict(list)
    signal_nodes_by_owner: dict[str, list[dict]] = defaultdict(list)
    for edge in all_edges:
        if edge.get("relation") != "contains":
            continue
        owner = nodes_by_id.get(str(edge.get("source") or ""))
        member = nodes_by_id.get(str(edge.get("target") or ""))
        if owner and member and _meta(member).get("godot_kind") == "script_property":
            property_nodes_by_owner[str(owner.get("id"))].append(member)
        if owner and member and _meta(member).get("godot_kind") == "script_signal":
            signal_nodes_by_owner[str(owner.get("id"))].append(member)

    def find_property(class_id: str, property_name: str) -> dict | None:
        seen: set[str] = set()
        current = class_id
        while current and current not in seen:
            seen.add(current)
            candidates = [
                member for member in property_nodes_by_owner.get(current, [])
                if str(member.get("label") or "") == property_name
            ]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return None
            bases = base_classes.get(current, [])
            if len(bases) != 1:
                return None
            current = bases[0]
        return None

    def find_signal(class_id: str, signal_name: str) -> dict | None:
        seen: set[str] = set()
        current = class_id
        while current and current not in seen:
            seen.add(current)
            candidates = [
                member for member in signal_nodes_by_owner.get(current, [])
                if str(member.get("label") or "") == signal_name
            ]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return None
            bases = base_classes.get(current, [])
            if len(bases) != 1:
                return None
            current = bases[0]
        return None

    def add_serialized_property_assignments(owner: dict, script: dict) -> None:
        owner_metadata = _meta(owner)
        properties = owner_metadata.get("godot_properties")
        if not isinstance(properties, dict):
            return
        property_lines = owner_metadata.get("godot_property_lines")
        property_lines = property_lines if isinstance(property_lines, dict) else {}
        script_id = str(script.get("id") or "")
        for property_name, value_summary in properties.items():
            target = find_property(script_id, str(property_name))
            if target is None:
                continue
            value_type = value_summary.get("type", "") if isinstance(value_summary, dict) else ""
            _append_reference_edge(
                all_edges,
                owner,
                target,
                relation="references",
                context="godot_serialized_property_assignment",
                line=int(property_lines.get(property_name) or 1),
                property_name=str(property_name),
                reference_kind="serialized_assignment",
                extra={"godot_serialized_value_type": str(value_type)[:120]},
            )

    for owner_id, script in resource_script_attachments.items():
        owner = nodes_by_id.get(owner_id)
        if owner:
            add_serialized_property_assignments(owner, script)

    for (resource_key, scene_path), script in attached_scripts.items():
        owner = entities_by_file.get(resource_key, {}).get(f"node:{scene_path}")
        if owner:
            add_serialized_property_assignments(owner, script)

    # Named property accessors are explicit implementation links. Inline
    # accessors remain described on the property node because they do not have
    # independent method definitions in the source graph.
    for owner_id, properties in property_nodes_by_owner.items():
        for prop in properties:
            metadata = _meta(prop)
            prop_name = str(prop.get("label") or "")
            for accessor_kind in ("getter", "setter"):
                accessor_name = str(metadata.get(f"godot_{accessor_kind}") or "")
                if not accessor_name or accessor_name == "<inline>":
                    continue
                candidates = method_named(owner_id, accessor_name)
                if len(candidates) == 1:
                    _append_reference_edge(
                        all_edges,
                        prop,
                        candidates[0],
                        relation="references",
                        context=f"godot_script_property_{accessor_kind}",
                        line=int(str(prop.get("source_location") or "L1").lstrip("L") or 1),
                        property_name=prop_name,
                        reference_kind="property_accessor",
                    )

    for file in script_files:
        facts = file["facts"]
        references = facts.get("property_references")
        if not isinstance(references, list):
            continue
        script_node = _node_target(nodes_by_resource_path.get(file["key"], []), file["res_path"])
        if script_node is None:
            continue
        for reference in references:
            if not isinstance(reference, dict):
                continue
            owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
            owner_id = str(script_node.get("id") or "")
            if owner_line != str(script_node.get("source_location") or ""):
                owner_candidates = [
                    node for node in nodes_by_resource_path.get(file["key"], [])
                    if _meta(node).get("godot_kind") == "script_inner_class"
                    and str(node.get("source_location") or "") == owner_line
                ]
                if len(owner_candidates) != 1:
                    continue
                owner_id = str(owner_candidates[0].get("id") or "")
            caller_name = str(reference.get("caller_name") or "")
            caller_line = f"L{max(1, int(reference.get('caller_line') or 1))}"
            caller_candidates = [
                method for method in method_named(owner_id, caller_name)
                if str(method.get("source_location") or "") == caller_line
            ]
            if len(caller_candidates) != 1:
                continue
            property_name = str(reference.get("property") or "")
            receiver_kind = str(reference.get("receiver_kind") or "self")
            receiver_name = str(reference.get("receiver") or "")
            property_owner_ids: list[str] = []
            if receiver_kind in {"self", "unqualified"}:
                property_owner_ids = [owner_id]
            elif receiver_kind == "super":
                bases = base_classes.get(owner_id, [])
                if len(bases) == 1:
                    property_owner_ids = [bases[0]]
            elif receiver_kind == "class":
                class_matches = global_classes_by_project.get((file["project_id"], receiver_name), [])
                if len(class_matches) == 1:
                    property_owner_ids = [str(class_matches[0].get("id") or "")]
                else:
                    aliases = preload_aliases_by_scope.get((file["key"], owner_line, receiver_name), [])
                    if len(aliases) == 1:
                        property_owner_ids = [str(aliases[0]["script"].get("id") or "")]
            elif receiver_kind == "identifier":
                bindings = [
                    binding for binding in facts.get("receiver_bindings", [])
                    if isinstance(binding, dict)
                    and str(binding.get("name") or "") == receiver_name
                    and str(binding.get("owner_class_line") or "") == str(reference.get("owner_class_line") or "")
                    and (not binding.get("caller_line") or str(binding.get("caller_line")) == str(reference.get("caller_line")))
                ]
                if not bindings:
                    bindings = [
                        binding for binding in facts.get("receiver_bindings", [])
                        if isinstance(binding, dict)
                        and str(binding.get("name") or "") == receiver_name
                        and str(binding.get("owner_class_line") or "") == str(reference.get("owner_class_line") or "")
                        and not binding.get("caller_line")
                    ]
                type_names = {str(binding.get("type") or "").strip() for binding in bindings}
                if len(type_names) == 1:
                    type_name = next(iter(type_names))
                    type_matches = classes_by_project.get((file["project_id"], type_name), [])
                    if len(type_matches) == 1:
                        property_owner_ids = [str(type_matches[0].get("id") or "")]
                    elif not type_matches:
                        aliases = preload_aliases_by_scope.get((file["key"], owner_line, type_name), [])
                        if len(aliases) == 1:
                            property_owner_ids = [str(aliases[0]["script"].get("id") or "")]
                if not property_owner_ids:
                    scene_targets = node_receiver_script_targets.get((file["key"][1], owner_line, caller_line, receiver_name), set())
                    if len(scene_targets) == 1:
                        property_owner_ids = list(scene_targets)
                if not property_owner_ids:
                    autoloads = autoload_scripts_by_name.get((file["project_id"], receiver_name), [])
                    if len(autoloads) == 1:
                        property_owner_ids = [str(autoloads[0].get("id") or "")]
            property_candidates = []
            seen_property_ids: set[str] = set()
            for property_owner_id in property_owner_ids:
                candidate = find_property(property_owner_id, property_name)
                if candidate is not None and str(candidate.get("id") or "") not in seen_property_ids:
                    seen_property_ids.add(str(candidate.get("id") or ""))
                    property_candidates.append(candidate)
            if len(property_candidates) != 1:
                continue
            if receiver_kind == "class" and not _meta(property_candidates[0]).get("godot_static"):
                continue
            access = str(reference.get("access") or "read")
            _append_reference_edge(
                all_edges,
                caller_candidates[0],
                property_candidates[0],
                relation="references",
                context=f"godot_script_property_{access}",
                line=int(reference.get("line") or 1),
                property_name=property_name,
                reference_kind=access,
                extra={"godot_receiver": receiver_name[:160]} if receiver_name else None,
            )

    for file in script_files:
        facts = file["facts"]
        references = facts.get("signal_references")
        if not isinstance(references, list):
            continue
        script_node = _node_target(nodes_by_resource_path.get(file["key"], []), file["res_path"])
        if script_node is None:
            continue
        for reference in references:
            if not isinstance(reference, dict):
                continue
            owner_line = f"L{max(1, int(reference.get('owner_class_line') or 1))}"
            owner_id = str(script_node.get("id") or "")
            if owner_line != str(script_node.get("source_location") or ""):
                owner_candidates = [
                    node for node in nodes_by_resource_path.get(file["key"], [])
                    if _meta(node).get("godot_kind") == "script_inner_class"
                    and str(node.get("source_location") or "") == owner_line
                ]
                if len(owner_candidates) != 1:
                    continue
                owner_id = str(owner_candidates[0].get("id") or "")
            caller_line = f"L{max(1, int(reference.get('caller_line') or 1))}"
            caller_name = str(reference.get("caller_name") or "")
            callers = [method for method in method_named(owner_id, caller_name)
                       if str(method.get("source_location") or "") == caller_line]
            signal_name = str(reference.get("signal") or "")
            signal_node = find_signal(owner_id, signal_name)
            if len(callers) != 1 or signal_node is None:
                continue
            event = str(reference.get("event") or "")
            _append_reference_edge(
                all_edges,
                callers[0],
                signal_node,
                relation="references",
                context="godot_script_signal_emit" if event == "emit" else "godot_script_signal_connection",
                line=int(reference.get("line") or 1),
                property_name=signal_name,
                reference_kind=event,
            )
            callback_name = str(reference.get("callback") or "")
            if event == "connect" and callback_name:
                callback = find_method(owner_id, callback_name)
                if callback is not None:
                    _append_reference_edge(
                        all_edges,
                        signal_node,
                        callback,
                        relation="references",
                        context="godot_script_signal_callback",
                        line=int(reference.get("line") or 1),
                        property_name=signal_name,
                        reference_kind="callback",
                        extra={"godot_callback_method": callback_name[:160]},
                    )
            elif event == "connect" and reference.get("callback_dynamic"):
                _append_script_diagnostic(file["node"], f"Signal {signal_name!r} callback remains dynamic; callback syntax does not identify one method on a known receiver.", int(reference.get("line") or 1))

    # The runner inventory is a useful verification boundary in Godot projects:
    # connect each explicit CSV row to the script file it names. Keep this path
    # sibling-relative and require one exact indexed script-class target.
    script_classes_by_source: dict[str, list[dict]] = defaultdict(list)
    for node in all_nodes:
        if _meta(node).get("godot_kind") != "script_class" or not node.get("source_file"):
            continue
        script_classes_by_source[_source_path_key(node["source_file"])].append(node)

    verification_entries_by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for node in all_nodes:
        entry = _meta(node).get("verification_test")
        if not isinstance(entry, dict) or not node.get("source_file"):
            continue
        key = (
            _source_path_key(node["source_file"]),
            str(node.get("source_location") or ""),
            str(entry.get("script") or ""),
        )
        verification_entries_by_key[key].append(node)

    for manifest in verification_manifests:
        manifest_node = manifest["node"]
        manifest_path = _source_path_key(manifest_node.get("source_file"))
        manifest_dir = posixpath.dirname(manifest_path)
        facts = manifest["facts"]
        diagnostics = facts.setdefault("resolution_diagnostics", [])
        for entry in facts.get("entries", []):
            if not isinstance(entry, dict):
                continue
            script_path = str(entry.get("script") or "").replace("\\", "/").strip()
            script_suffix = PurePosixPath(script_path).suffix.lower()
            unsafe_path = (
                not script_path
                or script_path.startswith("/")
                or ":" in script_path
                or ".." in PurePosixPath(script_path).parts
            )
            if unsafe_path:
                diagnostic = {
                    "kind": "verification_manifest_invalid_script",
                    "line": int(entry.get("line") or 1),
                    "message": f"Manifest script path {script_path!r} is not a safe relative path.",
                }
                if diagnostic not in diagnostics and len(diagnostics) < 200:
                    diagnostics.append(diagnostic)
                continue
            if script_suffix and script_suffix != ".gd":
                diagnostic = {
                    "kind": "verification_manifest_invalid_script",
                    "line": int(entry.get("line") or 1),
                    "message": f"Manifest script path {script_path!r} does not name a GDScript file.",
                }
                if diagnostic not in diagnostics and len(diagnostics) < 200:
                    diagnostics.append(diagnostic)
                continue
            if not script_suffix:
                script_path += ".gd"
            target_path = posixpath.normpath(posixpath.join(manifest_dir, script_path))
            candidates = script_classes_by_source.get(_source_path_key(target_path), [])
            if len(candidates) != 1:
                diagnostic = {
                    "kind": "verification_manifest_unresolved_script",
                    "line": int(entry.get("line") or 1),
                    "message": (
                        f"Manifest script {script_path!r} matches {len(candidates)} indexed GDScript classes."
                    ),
                }
                if diagnostic not in diagnostics and len(diagnostics) < 200:
                    diagnostics.append(diagnostic)
                continue
            extra = {
                f"verification_{key}": str(entry[key])[:500]
                for key in ("role", "state", "owner", "target", "load")
                if entry.get(key)
            }
            entry_location = f"L{int(entry.get('line') or 1)}"
            entry_nodes = verification_entries_by_key.get(
                (manifest_path, entry_location, str(entry.get("script") or "")), []
            )
            edge_source = entry_nodes[0] if len(entry_nodes) == 1 else manifest_node
            relation = "references" if str(entry.get("role") or "").casefold() == "report" else "tests"
            _append_reference_edge(
                all_edges,
                edge_source,
                candidates[0],
                relation=relation,
                context="verification_manifest_entry",
                line=int(entry.get("line") or 1),
                property_name="script",
                reference_kind="test_inventory",
                raw_path=script_path,
                extra=extra,
            )

    # NodePath properties resolve from their owner. Root inherited scenes share
    # the explicit base-scene tree; ordinary nested instances remain opaque.
    for file in files:
        key = file["key"]
        if not key or file["facts"].get("file_kind") != "scene":
            continue
        local_entities = entities_by_file.get(key, {})
        scene_nodes = scene_nodes_for(key)
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
        scene_nodes = scene_nodes_for(key)
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
            script_node = attached_script_for(key, target_path)
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
