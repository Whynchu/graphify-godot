#!/usr/bin/env python3
"""Benchmark repeatable cold and incremental Graphify updates on a Godot corpus.

The script copies Graphify-supported text sources into a temporary project before
editing them. It never changes the supplied corpus. The result JSON keeps update,
report/export, and query measurements separate.

Example:
    python tools/benchmark_godot_update.py --corpus ../ --file-limit 120 \
        --result .tools/godot-benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version as distribution_version
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS  # noqa: E402
from graphify.watch import _canonical_topology_for_compare  # noqa: E402

_SKIP_DIRS = {
    ".git", ".godot", ".tools", ".venv", "venv", "graphify-godot",
    "graphify-out", "__pycache__", ".pytest_cache", "node_modules",
}
_EXTRA_EXTENSIONS = {".csv"}
_TIMING_LINE = re.compile(
    r"^\[graphify timing\] (.+?): ([0-9]+(?:\.[0-9]+)?)s(?: \((.*)\))?$"
)


def _memory_bytes(pid: int) -> int | None:
    """Sample resident memory for the process and its descendants."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        class _ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD,
        ]
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return None
        parents: dict[int, int] = {}
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)

        process_ids = {pid}
        while True:
            children = {child for child, parent in parents.items() if parent in process_ids}
            new_ids = children - process_ids
            if not new_ids:
                break
            process_ids.update(new_ids)
        total = 0
        found = False
        for process_id in process_ids:
            handle = kernel32.OpenProcess(0x0400 | 0x0010, False, process_id)
            if not handle:
                continue
            try:
                counters = _Counters()
                counters.cb = ctypes.sizeof(counters)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    total += int(counters.WorkingSetSize)
                    found = True
            finally:
                kernel32.CloseHandle(handle)
        return total if found else None

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        parents: dict[int, int] = {}
        rss_by_pid: dict[int, int] = {}
        for entry_path in Path("/proc").iterdir():
            if not entry_path.name.isdigit():
                continue
            try:
                fields = (entry_path / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                process_id = int(entry_path.name)
                parents[process_id] = int(fields[1])
                rss_by_pid[process_id] = int(fields[21]) * page_size
            except (OSError, ValueError, IndexError):
                continue
        process_ids = {pid}
        while True:
            children = {child for child, parent in parents.items() if parent in process_ids}
            new_ids = children - process_ids
            if not new_ids:
                break
            process_ids.update(new_ids)
        values = [rss_by_pid[item] for item in process_ids if item in rss_by_pid]
        return sum(values) if values else None
    except (OSError, ValueError, KeyError):
        return None


def _copy_corpus(source: Path, destination: Path, file_limit: int | None) -> dict:
    supported = CODE_EXTENSIONS | DOC_EXTENSIONS | _EXTRA_EXTENSIONS
    candidates: list[Path] = []
    for current, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        base = Path(current)
        for name in names:
            path = base / name
            if path.suffix.lower() in supported or name in (".graphifyignore", ".gitignore"):
                candidates.append(path)
    candidates.sort(key=lambda p: p.relative_to(source).as_posix().casefold())

    if file_limit is not None and len(candidates) > file_limit:
        godot_groups = {
            suffix: [p for p in candidates if p.suffix.lower() == suffix]
            for suffix in (".gd", ".tres", ".tscn")
        }
        if any(not godot_groups[suffix] for suffix in godot_groups):
            missing = ", ".join(suffix for suffix, group in godot_groups.items() if not group)
            raise ValueError(f"--file-limit corpus is missing required samples: {missing}")
        other = [p for p in candidates if p not in set().union(*godot_groups.values())]
        # Keep the project config and one example of each Godot source group,
        # then sample evenly through each sorted group instead of taking only
        # alphabetically early add-ons or documentation.
        required = [group[0] for group in godot_groups.values()]
        project_config = next((p for p in other if p.name == "project.godot"), None)
        if project_config is not None:
            required.append(project_config)
        if len(required) > file_limit:
            raise ValueError("--file-limit must leave room for one .gd, .tres, .tscn, and project config")

        def spread(group: list[Path], count: int) -> list[Path]:
            if count <= 0:
                return []
            if count >= len(group):
                return group
            if count == 1:
                return [group[len(group) // 2]]
            indexes = {round(i * (len(group) - 1) / (count - 1)) for i in range(count)}
            return [group[i] for i in sorted(indexes)]

        remaining = file_limit - len(required)
        extra_gd = round(remaining * 0.55)
        extra_tres = round(remaining * 0.20)
        extra_tscn = round(remaining * 0.15)
        quotas = {
            ".gd": 1 + extra_gd,
            ".tres": 1 + extra_tres,
            ".tscn": 1 + extra_tscn,
            "<other>": remaining - extra_gd - extra_tres - extra_tscn,
        }
        selected = set(required)
        for suffix, quota in quotas.items():
            group = other if suffix == "<other>" else godot_groups[suffix]
            picked = spread(group, min(quota, len(group)))
            selected.update(picked)
        for group in (godot_groups[".gd"], godot_groups[".tres"], godot_groups[".tscn"], other):
            if len(selected) >= file_limit:
                break
            for path in spread(group, min(len(group), file_limit - len(selected))):
                selected.add(path)
                if len(selected) >= file_limit:
                    break
        candidates = sorted(selected, key=lambda p: p.relative_to(source).as_posix().casefold())

    for path in candidates:
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    counts: dict[str, int] = {}
    total_bytes = 0
    for path in candidates:
        counts[path.suffix.lower() or "<none>"] = counts.get(path.suffix.lower() or "<none>", 0) + 1
        total_bytes += path.stat().st_size
    return {
        "source_root": str(source),
        "selected_files": len(candidates),
        "selected_bytes": total_bytes,
        "extension_counts": dict(sorted(counts.items())),
        "file_limit": file_limit,
        "excluded_directories": sorted(_SKIP_DIRS),
        "mode": "code-only; Graphify CODE_EXTENSIONS + DOC_EXTENSIONS + .csv copied; docs are discovered but not semantically extracted",
    }


def _run(label: str, command: list[str], project: Path, graph_out: Path, work: Path) -> dict:
    env = os.environ.copy()
    env["GRAPHIFY_OUT"] = str(graph_out.resolve())
    old_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + old_pythonpath if old_pythonpath else "")
    stdout_path = work / f"{label}.stdout.txt"
    stderr_path = work / f"{label}.stderr.txt"
    started = time.perf_counter()
    peak = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=REPO_ROOT, env=env, stdout=stdout, stderr=stderr,
        )
        while process.poll() is None:
            measured = _memory_bytes(process.pid)
            if measured is not None:
                peak = max(peak or 0, measured)
            time.sleep(0.025)
        return_code = process.returncode
    wall_seconds = time.perf_counter() - started
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    if return_code:
        tail = (stdout_text + "\n" + stderr_text)[-6000:]
        raise RuntimeError(f"{label} failed with exit {return_code}:\n{tail}")
    timings = []
    for line in (stdout_text + "\n" + stderr_text).splitlines():
        match = _TIMING_LINE.match(line.strip())
        if match:
            timings.append({
                "stage": match.group(1),
                "seconds": float(match.group(2)),
                "detail": match.group(3),
            })
    return {
        "label": label,
        "command": command,
        "wall_seconds": wall_seconds,
        "peak_rss_bytes": peak,
        "peak_rss_source": "sampled Windows/Linux process-tree resident memory",
        "return_code": return_code,
        "timings": timings,
    }


def _edit_sample(project: Path, suffix: str, marker: str) -> str:
    path = next((p for p in sorted(project.rglob(f"*{suffix}")) if p.is_file()), None)
    if path is None:
        raise ValueError(f"benchmark corpus has no {suffix} sample")
    comment = f"\n# {marker}\n" if suffix == ".gd" else f"\n; {marker}\n"
    with path.open("ab") as stream:
        stream.write(comment.encode("utf-8"))
    return path.relative_to(project).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="Godot project/corpus root")
    parser.add_argument("--result", type=Path, default=Path("godot-benchmark.json"))
    parser.add_argument("--scratch", type=Path, help="parent for temporary corpus and graph outputs")
    parser.add_argument("--file-limit", type=int, help="optional deterministic source-file cap")
    parser.add_argument("--max-workers", type=int, help="optional extractor worker cap for comparable runs")
    parser.add_argument("--query", default="GDScript scene resource calls", help="separate query-speed probe")
    parser.add_argument("--keep-workdir", action="store_true", help="copy temporary graph outputs for parity diagnosis")
    args = parser.parse_args()

    source = args.corpus.expanduser().resolve()
    if not source.is_dir():
        parser.error(f"corpus directory not found: {source}")
    source_version = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    installed_version = distribution_version("graphifyy")
    if installed_version != source_version:
        parser.error(
            f"active graphifyy metadata is {installed_version}, but this checkout is "
            f"{source_version}; reinstall the local checkout before measuring"
        )
    if args.file_limit is not None and args.file_limit < 1:
        parser.error("--file-limit must be positive")
    if args.max_workers is not None and args.max_workers < 1:
        parser.error("--max-workers must be positive")
    if args.scratch:
        args.scratch.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="graphify-godot-bench-", dir=args.scratch) as temp:
        work = Path(temp)
        project = work / "corpus"
        incremental_out = work / "incremental-graphify-out"
        full_out = work / "full-graphify-out"
        corpus_config = _copy_corpus(source, project, args.file_limit)
        python = sys.executable
        base = [python, "-m", "graphify"]
        results = []

        cases = [
            ("cold", None),
            ("unchanged", None),
            ("gdscript-edit", (".gd", "Graphify benchmark GDScript edit")),
            ("resource-edit", (".tres", "Graphify benchmark resource edit")),
            ("scene-edit", (".tscn", "Graphify benchmark scene edit")),
        ]
        edited_paths = []
        for label, edit in cases:
            if edit:
                edited_paths.append(_edit_sample(project, *edit))
            command = base + ["extract", str(project), "--code-only", "--timing"]
            if args.max_workers is not None:
                command += ["--max-workers", str(args.max_workers)]
            results.append(_run(label, command, project, incremental_out, work))

        report_command = base + [
            "cluster-only", str(project), "--no-label", "--timing",
            "--graph", str(incremental_out / "graph.json"),
        ]
        report_result = _run("report-and-html", report_command, project, incremental_out, work)

        query_command = base + ["query", args.query]
        query_result = _run("query", query_command, project, incremental_out, work)

        full_command = base + ["extract", str(project), "--code-only", "--force", "--timing"]
        if args.max_workers is not None:
            full_command += ["--max-workers", str(args.max_workers)]
        full_result = _run("full-rebuild", full_command, project, full_out, work)
        incremental_graph = json.loads((incremental_out / "graph.json").read_text(encoding="utf-8"))
        full_graph = json.loads((full_out / "graph.json").read_text(encoding="utf-8"))
        parity = _canonical_topology_for_compare(incremental_graph) == _canonical_topology_for_compare(full_graph)
        parity_result = {
            "passed": parity,
            "incremental_nodes": len(incremental_graph.get("nodes", [])),
            "full_nodes": len(full_graph.get("nodes", [])),
            "incremental_links": len(incremental_graph.get("links", incremental_graph.get("edges", []))),
            "full_links": len(full_graph.get("links", full_graph.get("edges", []))),
        }
        if not parity:
            incremental_norm = _canonical_topology_for_compare(incremental_graph)
            full_norm = _canonical_topology_for_compare(full_graph)
            incremental_links = {
                json.dumps(edge, sort_keys=True, ensure_ascii=False)
                for edge in incremental_norm.get("links", incremental_norm.get("edges", []))
            }
            full_links = {
                json.dumps(edge, sort_keys=True, ensure_ascii=False)
                for edge in full_norm.get("links", full_norm.get("edges", []))
            }
            parity_result["only_incremental_links"] = [
                json.loads(edge) for edge in sorted(incremental_links - full_links)[:20]
            ]
            parity_result["only_full_links"] = [
                json.loads(edge) for edge in sorted(full_links - incremental_links)[:20]
            ]
        html_path = incremental_out / "graph.html"
        result = {
            "schema_version": 1,
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "python": sys.version,
            "analyzer_package_version": installed_version,
            "analyzer_source_version": source_version,
            "installed_version_matches_source": installed_version == source_version,
            "analyzer_checkout": str(REPO_ROOT),
            "corpus": corpus_config,
            "max_workers": args.max_workers,
            "edited_paths": edited_paths,
            "updates": results,
            "report_and_html": report_result,
            "query_separate_from_update": query_result,
            "full_rebuild": full_result,
            "normalized_incremental_full_parity": parity_result,
            "graph_html_written": html_path.exists(),
        }
        result_path = args.result.expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if args.keep_workdir:
            keep_path = result_path.with_suffix(".work")
            suffix = 1
            while keep_path.exists():
                keep_path = result_path.with_suffix(f".work-{suffix}")
                suffix += 1
            shutil.copytree(work, keep_path)
            result["kept_workdir"] = str(keep_path)
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({
            "result": str(result_path),
            "selected_files": corpus_config["selected_files"],
            "updates": [{
                "case": item["label"],
                "wall_seconds": round(item["wall_seconds"], 3),
                "peak_rss_bytes": item["peak_rss_bytes"],
            } for item in results],
            "report_seconds": round(report_result["wall_seconds"], 3),
            "query_seconds": round(query_result["wall_seconds"], 3),
            "normalized_incremental_full_parity": parity_result,
            "graph_html_written": result["graph_html_written"],
            "kept_workdir": result.get("kept_workdir"),
        }, indent=2))
        return 0 if parity else 2


if __name__ == "__main__":
    raise SystemExit(main())
