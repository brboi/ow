"""Tests for Zed plugin."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping
from types import ModuleType

from ow.core.plugins import hookimpl, load_plugins
from ow.specs.editors import DebugLaunch, EditorContribution, EditorsSpec


@dataclass(frozen=True)
class FakeWorkspace:
    """Minimal workspace for testing."""

    root: Path
    name: str
    settings: Mapping[str, object]


def test_zed_settings_json_structure(tmp_path):
    """Test that Zed settings.json has correct structure."""
    # Create stub that provides contribution via editors_contribution hook
    stub = ModuleType("stub")
    stub.HOOKSPECS = (EditorsSpec,)
    
    @hookimpl
    def editors_contribution(ws):
        return EditorContribution(
            language_servers={"python": {"key": "value"}},
            excludes=("**/*.po",),
        )
    
    stub.editors_contribution = editors_contribution
    
    # Load plugins with zed
    from ow.plugins import zed as zed_plugin
    
    plugins = load_plugins((
        ("stub", stub),
        ("zed", zed_plugin),
    ))
    
    ws = FakeWorkspace(
        root=tmp_path,
        name="test",
        settings={"stub.enabled": True, "zed.enabled": True},
    )
    
    # Get files through ow_generated_files
    files = []
    for _name, result in plugins.call("ow_generated_files", ws=ws, answers=plugins.answers(ws)):
        files.extend(result)
    
    # Find settings.json
    settings_file = next(f for f in files if f.path == PurePosixPath(".zed/settings.json"))
    content = json.loads(settings_file.data.decode("utf-8"))
    
    # Verify structure
    assert "lsp" in content
    assert "python" in content["lsp"]
    assert "settings" in content["lsp"]["python"]
    assert content["lsp"]["python"]["settings"]["key"] == "value"
    
    assert "file_scan_exclusions" in content
    assert isinstance(content["file_scan_exclusions"], list)
    assert "**/.venv" in content["file_scan_exclusions"]
    assert "**/node_modules" in content["file_scan_exclusions"]
    assert "**/*.po" in content["file_scan_exclusions"]


def test_zed_debug_json_structure(tmp_path):
    """Test that Zed debug.json has correct structure."""
    # Create stub with launch via editors_contribution hook
    stub = ModuleType("stub")
    stub.HOOKSPECS = (EditorsSpec,)
    
    @hookimpl
    def editors_contribution(ws):
        return EditorContribution(
            launches=(
                DebugLaunch(
                    name="Test Launch",
                    program=tmp_path / "prog.py",
                    args=("arg1", "arg2"),
                    cwd=tmp_path,
                    env={"KEY": "value"},
                ),
            ),
        )
    
    stub.editors_contribution = editors_contribution
    
    from ow.plugins import zed as zed_plugin
    
    plugins = load_plugins((
        ("stub", stub),
        ("zed", zed_plugin),
    ))
    
    ws = FakeWorkspace(
        root=tmp_path,
        name="test",
        settings={"stub.enabled": True, "zed.enabled": True},
    )
    
    files = []
    for _name, result in plugins.call("ow_generated_files", ws=ws, answers=plugins.answers(ws)):
        files.extend(result)
    
    # Find debug.json
    debug_file = next(f for f in files if f.path == PurePosixPath(".zed/debug.json"))
    content = json.loads(debug_file.data.decode("utf-8"))
    
    # Verify structure - should be array
    assert isinstance(content, list)
    assert len(content) == 1
    
    config = content[0]
    assert config["label"] == "Test Launch"
    assert config["adapter"] == "Debugpy"
    assert config["request"] == "launch"
    assert config["console"] == "integratedTerminal"
    assert config["justMyCode"] is False
    assert config["pythonArgs"] == ["-Xfrozen_modules=off"]
    assert "python" in config
    assert ".venv/bin/python" in config["python"]
    assert config["args"] == ["arg1", "arg2"]
    assert config["env"]["KEY"] == "value"


def test_zed_excludes_deduplication(tmp_path):
    """Test that Zed deduplicates excludes while preserving order."""
    # Create two stubs with overlapping excludes
    stub1 = ModuleType("stub1")
    stub1.HOOKSPECS = (EditorsSpec,)
    
    @hookimpl
    def editors_contribution_1(ws):
        return EditorContribution(excludes=("**/*.po", "**/dist"))
    
    stub1.editors_contribution = editors_contribution_1
    
    stub2 = ModuleType("stub2")
    stub2.HOOKSPECS = (EditorsSpec,)
    
    @hookimpl
    def editors_contribution_2(ws):
        return EditorContribution(excludes=("**/*.po", "**/build"))
    
    stub2.editors_contribution = editors_contribution_2
    
    from ow.plugins import zed as zed_plugin
    
    plugins = load_plugins((
        ("stub1", stub1),
        ("stub2", stub2),
        ("zed", zed_plugin),
    ))
    
    ws = FakeWorkspace(
        root=tmp_path,
        name="test",
        settings={"stub1.enabled": True, "stub2.enabled": True, "zed.enabled": True},
    )
    
    files = []
    for _name, result in plugins.call("ow_generated_files", ws=ws, answers=plugins.answers(ws)):
        files.extend(result)
    
    settings_file = next(f for f in files if f.path == PurePosixPath(".zed/settings.json"))
    content = json.loads(settings_file.data.decode("utf-8"))
    
    # Check deduplication and order
    excludes = content["file_scan_exclusions"]
    # *.po appears only once, order preserved
    po_count = sum(1 for e in excludes if "*.po" in e)
    assert po_count == 1
    
    # All custom excludes present
    assert "**/dist" in excludes
    assert "**/build" in excludes
