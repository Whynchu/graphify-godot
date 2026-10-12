#!/usr/bin/env python3
"""Smoke-test the installed Graphify package against a minimal Godot project."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    python = repo_root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        raise SystemExit(f"isolated environment does not exist: {python.parent}")

    with tempfile.TemporaryDirectory(prefix="graphify-godot-package-") as temporary:
        work = Path(temporary)
        project = work / "project"
        output = work / "output"
        project.mkdir()
        (project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (project / "sample.gd").write_text(
            "class_name PackageSmoke\n"
            "extends Node\n"
            "func launch() -> void:\n"
            "    helper()\n"
            "func helper() -> void:\n"
            "    pass\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                str(python), "-m", "graphify", "extract", str(project),
                "--code-only", "--no-cluster", "--out", str(output),
            ],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise SystemExit(
                f"installed Graphify extraction failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        graph_path = output / "graphify-out" / "graph.json"
        if not graph_path.is_file():
            raise SystemExit(f"installed Graphify did not write {graph_path}")
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", graph.get("links", []))
        script_class = any(
            node.get("metadata", {}).get("godot_kind") == "script_class"
            and node.get("metadata", {}).get("godot_global_class") is True
            for node in nodes
        )
        if not script_class:
            raise SystemExit("installed [godot] package did not extract the GDScript class")
        if not any(edge.get("relation") == "calls" for edge in edges):
            raise SystemExit("installed [godot] package did not resolve the same-script call")

    print("Installed package extracted a GDScript class and its method call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
