"""Extractor for bounded CSV test inventories recognized by Graphify."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from graphify.extractors.base import _make_id
from graphify.verification_manifest import is_verification_manifest_csv


_MAX_ENTRIES = 20_000
_MAX_FIELD_CHARS = 2_000
_SCRIPT_TOKEN = re.compile(r"^[A-Za-z0-9_.\\/-]{1,260}$")
_METADATA_COLUMNS = ("role", "state", "owner", "target", "load", "notes")


def extract_verification_manifest(path: Path) -> dict:
    """Index each inventory row as an explicit link to its smoke-test script."""
    if not is_verification_manifest_csv(path):
        return {"nodes": [], "edges": [], "skipped": "not a verification manifest CSV"}
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return {"nodes": [], "edges": [], "error": "verification manifest exceeds 16 MiB"}
        entries: list[dict] = []
        diagnostics: list[dict] = []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            for row_number, row in enumerate(reader, start=2):
                if row_number > _MAX_ENTRIES + 1:
                    diagnostics.append({
                        "kind": "verification_manifest_limit",
                        "message": f"Only the first {_MAX_ENTRIES} inventory rows were indexed.",
                        "line": row_number,
                    })
                    break
                script = str(row.get("script") or "").strip()
                if not _SCRIPT_TOKEN.fullmatch(script):
                    diagnostics.append({
                        "kind": "verification_manifest_invalid_script",
                        "message": "Inventory row has an empty or unsupported script path.",
                        "line": row_number,
                    })
                    continue
                entry = {"script": script, "line": row_number}
                for column in _METADATA_COLUMNS:
                    value = str(row.get(column) or "").strip()
                    if value:
                        entry[column] = value[:_MAX_FIELD_CHARS]
                entries.append(entry)
    except (OSError, UnicodeError, csv.Error) as exc:
        return {"nodes": [], "edges": [], "error": f"invalid verification manifest: {exc}"}

    source_file = str(path)
    file_node = {
        "id": _make_id(source_file),
        "label": path.name,
        "file_type": "code",
        "source_file": source_file,
        "source_location": "L1",
        "metadata": {},
    }
    file_node["metadata"]["verification_manifest"] = {
        "row_count": len(entries),
        "entries": entries,
        "diagnostics": diagnostics[:200],
    }
    nodes = [file_node]
    edges = []
    for entry in entries:
        line = int(entry["line"])
        entry_id = _make_id(source_file, "verification_entry", str(line))
        entry_metadata = {
            key: value for key, value in entry.items()
            if key != "line"
        }
        nodes.append({
            "id": entry_id,
            "label": entry["script"],
            "file_type": "code",
            "source_file": source_file,
            "source_location": f"L{line}",
            "metadata": {"verification_test": entry_metadata},
        })
        edges.append({
            "source": file_node["id"],
            "target": entry_id,
            "relation": "contains",
            "confidence": "EXTRACTED",
            "source_file": source_file,
            "source_location": f"L{line}",
            "weight": 1.0,
        })
    return {"nodes": nodes, "edges": edges}
