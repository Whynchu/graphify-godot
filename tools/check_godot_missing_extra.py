#!/usr/bin/env python3
"""Verify a base Graphify install explains how to enable GDScript support."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    python = repo_root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        raise SystemExit(f"isolated environment does not exist: {python.parent}")
    grammar = subprocess.run(
        [str(python), "-c", "import importlib.util; print(importlib.util.find_spec('tree_sitter_gdscript'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    if grammar.stdout.strip() != "None":
        raise SystemExit("no-extra lane unexpectedly has tree_sitter_gdscript installed")

    with tempfile.TemporaryDirectory(prefix="graphify-godot-no-extra-") as temporary:
        work = Path(temporary)
        project = work / "project"
        output = work / "output"
        project.mkdir()
        (project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (project / "sample.gd").write_text(
            "extends Node\nfunc ready() -> void:\n    pass\n", encoding="utf-8",
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
        hint = 'pip install "graphifyy[godot]"'
        if hint not in result.stderr:
            raise SystemExit(
                "missing-grammar output did not show the install hint "
                f"{hint!r}:\n{result.stderr}"
            )

    print("Missing GDScript grammar produced the expected [godot] install hint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
