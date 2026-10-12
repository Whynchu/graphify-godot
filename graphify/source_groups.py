"""Shared source-group classification for all standalone HTML views."""

from __future__ import annotations

import fnmatch
import json
import os
from functools import lru_cache
from pathlib import Path

SOURCE_GROUPS = (
    ("docs", "Docs"),
    ("tests", "Tests"),
    ("scripts", "Scripts"),
    ("scenes", "Scenes"),
    ("resources", "Resources"),
    ("tools", "Tools"),
    ("addons", "Addons"),
    ("config", "Config"),
    ("other", "Other"),
)
SOURCE_GROUP_IDS = {group_id for group_id, _label in SOURCE_GROUPS}


@lru_cache(maxsize=8)
def _source_group_overrides(raw: str) -> dict:
    try:
        overrides = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return overrides if isinstance(overrides, dict) else {}


def source_group_for_node(data: dict) -> str:
    """Classify a graph node by explicit override, source ownership, then suffix."""
    explicit = str(data.get("source_group") or "").strip().lower()
    if explicit in SOURCE_GROUP_IDS:
        return explicit

    source = str(data.get("source_file") or data.get("path") or "")
    normalized = source.replace("\\", "/").casefold()
    parts = [part for part in normalized.split("/") if part]
    suffix = Path(source).suffix.casefold()

    overrides = _source_group_overrides(os.environ.get("GRAPHIFY_SOURCE_GROUP_OVERRIDES", "{}"))
    candidates = [normalized]
    candidates.extend("/".join(parts[index:]) for index in range(len(parts)))
    for pattern, group in overrides.items():
        group_id = str(group).strip().lower()
        if group_id not in SOURCE_GROUP_IDS:
            continue
        normalized_pattern = str(pattern).replace("\\", "/").casefold().lstrip("/")
        if normalized_pattern and any(fnmatch.fnmatchcase(candidate, normalized_pattern) for candidate in candidates):
            return group_id

    if any(part in {"addons", "addon", "plugins", "plugin"} for part in parts):
        return "addons"
    if any(part in {"tools", "tooling", "scripts_tools"} for part in parts):
        return "tools"
    if any(part in {"tests", "test", "spec", "specs", "verification"} for part in parts):
        return "tests"
    if suffix in {".md", ".rst", ".adoc", ".txt"}:
        return "docs"
    if suffix == ".tscn":
        return "scenes"
    if suffix in {".tres", ".res", ".scn"}:
        return "resources"
    if suffix in {".godot", ".toml", ".ini", ".cfg", ".yaml", ".yml", ".json", ".csv", ".xml"}:
        return "config"
    if suffix in {
        ".gd", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".java", ".kt", ".kts",
        ".rs", ".go", ".rb", ".php", ".swift", ".lua", ".sh", ".ps1", ".sql",
    }:
        return "scripts"
    return "other"


def source_groups_for_node(data: dict) -> list[str]:
    """Return source groups represented by a node, including aggregate nodes."""
    raw = data.get("source_groups")
    if isinstance(raw, (list, tuple, set)):
        groups = sorted({str(value).strip().lower() for value in raw} & SOURCE_GROUP_IDS)
        if groups:
            return groups
    return [source_group_for_node(data)]

