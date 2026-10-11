"""Recognized verification inventories connect to their tests and dependencies."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.detect import FileType, classify_file
from graphify.extract import extract
from graphify.extractors.base import _make_id
from graphify.extractors.powershell import extract_powershell


def _write_project(root: Path) -> None:
    (root / "project.godot").write_text(
        '[application]\nconfig/name="VerificationFixture"\n', encoding="utf-8"
    )


def test_only_test_inventory_csv_is_classified_for_code_extraction(tmp_path: Path):
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        'script,role,state,owner,target,load\nsmoke,gate,verified,test,system,pure\n',
        encoding="utf-8",
    )
    data = tmp_path / "measurements.csv"
    data.write_text("time,value\n1,2\n", encoding="utf-8")

    assert classify_file(manifest) == FileType.CODE
    assert classify_file(data) is None


def test_manifest_links_smoke_script_to_its_explicit_gameplay_dependency(tmp_path: Path):
    pytest.importorskip("tree_sitter_gdscript")
    _write_project(tmp_path)
    tests_dir = tmp_path / "tests"
    scripts_dir = tmp_path / "scripts"
    tests_dir.mkdir()
    scripts_dir.mkdir()
    gameplay = scripts_dir / "gameplay_controller.gd"
    gameplay.write_text(
        "class_name GameplayController\nextends Node\n", encoding="utf-8"
    )
    smoke = tests_dir / "gameplay_smoke.gd"
    smoke.write_text(
        "extends SceneTree\n"
        'const GAMEPLAY = preload("res://scripts/gameplay_controller.gd")\n'
        "func _initialize() -> void:\n"
        "    pass\n",
        encoding="utf-8",
    )
    manifest = tests_dir / "manifest.csv"
    manifest.write_text(
        'script,role,state,owner,target,load,notes\n'
        'gameplay_smoke,gate,verified,combat,gameplay controller,pure,focused pass\n',
        encoding="utf-8",
    )

    result = extract([gameplay, smoke, manifest], root=tmp_path, parallel=False)
    nodes = {node["id"]: node for node in result["nodes"]}
    manifest_node = next(node for node in nodes.values() if node.get("source_file", "").endswith("manifest.csv"))
    smoke_class = next(
        node for node in nodes.values()
        if node.get("label") == "gameplay_smoke"
        and (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    gameplay_class = next(
        node for node in nodes.values()
        if node.get("label") == "GameplayController"
        and (node.get("metadata") or {}).get("godot_kind") == "script_class"
    )
    test_entry = next(
        node for node in nodes.values()
        if (node.get("metadata") or {}).get("verification_test", {}).get("script") == "gameplay_smoke"
    )
    assert any(
        edge.get("source") == manifest_node["id"]
        and edge.get("target") == test_entry["id"]
        and edge.get("relation") == "contains"
        for edge in result["edges"]
    )
    manifest_edge = next(
        edge for edge in result["edges"]
        if edge.get("source") == test_entry["id"]
        and edge.get("target") == smoke_class["id"]
        and edge.get("context") == "verification_manifest_entry"
    )
    assert manifest_edge["relation"] == "tests"
    assert manifest_edge["metadata"]["verification_role"] == "gate"
    assert manifest_edge["metadata"]["verification_state"] == "verified"
    assert any(
        edge.get("source") == smoke_class["id"]
        and edge.get("target") == gameplay_class["id"]
        and edge.get("context") == "godot_script_preload"
        for edge in result["edges"]
    )



def test_powershell_import_csv_links_to_the_manifest_file(tmp_path: Path):
    pytest.importorskip("tree_sitter_powershell")
    runner = tmp_path / "run_smoke.ps1"
    manifest = tmp_path / "manifest.csv"
    runner.write_text(
        '$manifestPath = Join-Path $PSScriptRoot "manifest.csv"\n'
        '$manifest = Import-Csv -LiteralPath $manifestPath\n',
        encoding="utf-8",
    )
    manifest.write_text(
        'script,role,state\nsmoke,gate,verified\n', encoding="utf-8"
    )

    result = extract_powershell(runner)
    runner_file = next(node for node in result["nodes"] if node["label"] == "run_smoke.ps1")
    assert any(
        edge.get("source") == runner_file["id"]
        and edge.get("target") == _make_id(str(manifest))
        and edge.get("relation") == "imports_from"
        and edge.get("context") == "powershell_import_csv"
        for edge in result["edges"]
    )
