"""Tests for VSCode plugin."""

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


def test_vscode_settings_json_structure(tmp_path):
    """Test that VSCode settings.json has correct structure."""
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
    
    # Load plugins with vscode
    from ow.plugins import vscode
    
    plugins = load_plugins((
        ("stub", stub),
        ("vscode", vscode),
    ))
    
    ws = FakeWorkspace(
        root=tmp_path,
        name="test",
        settings={"stub.enabled": True, "vscode.enabled": True},
    )
    
    # Get files through ow_generated_files
    files = []
    for _name, result in plugins.call("ow_generated_files", ws=ws, answers=plugins.answers(ws)):
        files.extend(result)
    
    # Find settings.json
    settings_file = next(f for f in files if f.path == PurePosixPath(".vscode/settings.json"))
    content = json.loads(settings_file.data.decode("utf-8"))
    
    # Verify structure
    assert "key" in content
    assert "search.exclude" in content
    assert isinstance(content["search.exclude"], dict)
    assert content["search.exclude"]["**/.venv"] is True


def test_vscode_launch_json_structure(tmp_path):
    """Test that VSCode launch.json has correct structure."""
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
    
    from ow.plugins import vscode
    
    plugins = load_plugins((
        ("stub", stub),
        ("vscode", vscode),
    ))
    
    ws = FakeWorkspace(
        root=tmp_path,
        name="test",
        settings={"stub.enabled": True, "vscode.enabled": True},
    )
    
    files = []
    for _name, result in plugins.call("ow_generated_files", ws=ws, answers=plugins.answers(ws)):
        files.extend(result)
    
    # Find launch.json
    launch_file = next(f for f in files if f.path == PurePosixPath(".vscode/launch.json"))
    content = json.loads(launch_file.data.decode("utf-8"))
    
    # Verify structure
    assert content["version"] == "0.2.0"
    assert isinstance(content["configurations"], list)
    assert len(content["configurations"]) == 1
    
    config = content["configurations"][0]
    assert config["name"] == "Test Launch"
    assert config["type"] == "debugpy"
    assert config["request"] == "launch"
    assert config["justMyCode"] is False
    assert config["pythonArgs"] == ["-Xfrozen_modules=off"]
    assert "python" in config
    assert ".venv/bin/python" in config["python"]
    assert config["args"] == ["arg1", "arg2"]
    assert config["env"]["KEY"] == "value"
