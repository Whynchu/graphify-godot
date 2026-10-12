#!/usr/bin/env python3
"""Install one freshly built Graphify wheel/sdist into the CI venv."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("format", choices=("wheel", "sdist"))
    parser.add_argument("--godot", action="store_true", help="install the [godot] extra")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    pattern = "*.whl" if args.format == "wheel" else "*.tar.gz"
    artifacts = sorted((repo_root / "dist").glob(pattern))
    if len(artifacts) != 1:
        parser.error(f"expected one {args.format} artifact in dist/, found {len(artifacts)}")

    venv = repo_root / ".venv"
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        parser.error(f"isolated environment does not exist: {venv}")
    requirement = str(artifacts[0].resolve()) + ("[godot]" if args.godot else "")
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", requirement], check=True)
    print(f"Installed {requirement} into {venv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
