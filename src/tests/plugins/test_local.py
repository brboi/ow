"""Tests for the local plugin."""

from __future__ import annotations

import pytest
from pathlib import Path

from ow.core.plugins import load_plugins
from ow.core.workspace import Workspace, Repo
from ow.plugins import local


def test_local_copies_missing_files(tmp_path, monkeypatch):
    """Test that local plugin copies missing files from seed dir."""
    # Setup
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    
    # Create seed directory with files
    seed_dir = tmp_path / "config" / "ow" / "local"
    seed_dir.mkdir(parents=True)
    (seed_dir / "file1.txt").write_text("content1")
    (seed_dir / "subdir").mkdir()
    (seed_dir / "subdir" / "file2.txt").write_text("content2")
    
    # Create workspace
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    
    ws = Workspace(
        root=ws_root,
        name="test",
        repos=(),
        settings={},
    )
    
    # Call the hook
    local.ow_workspace_created(ws)
    
    # Verify files were copied
    assert (ws_root / ".local" / "file1.txt").read_text() == "content1"
    assert (ws_root / ".local" / "subdir" / "file2.txt").read_text() == "content2"


def test_local_never_overwrites(tmp_path, monkeypatch):
    """Test that local plugin never overwrites existing files."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    
    # Create seed directory
    seed_dir = tmp_path / "config" / "ow" / "local"
    seed_dir.mkdir(parents=True)
    (seed_dir / "file1.txt").write_text("seed content")
    
    # Create workspace with existing file
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    local_dir = ws_root / ".local"
    local_dir.mkdir(parents=True)
    (local_dir / "file1.txt").write_text("existing content")
    
    ws = Workspace(
        root=ws_root,
        name="test",
        repos=(),
        settings={},
    )
    
    # Call the hook
    local.ow_workspace_created(ws)
    
    # Verify file was not overwritten
    assert (local_dir / "file1.txt").read_text() == "existing content"


def test_local_handles_missing_seed_dir(tmp_path, monkeypatch):
    """Test that local plugin handles missing seed directory gracefully."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    
    # Create workspace (no seed dir)
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    
    ws = Workspace(
        root=ws_root,
        name="test",
        repos=(),
        settings={},
    )
    
    # Call the hook - should not raise
    local.ow_workspace_created(ws)
    
    # .local directory should not be created if seed dir doesn't exist
    assert not (ws_root / ".local").exists()


def test_local_creates_directories(tmp_path, monkeypatch):
    """Test that local plugin creates parent directories as needed."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    
    # Create seed directory with nested files
    seed_dir = tmp_path / "config" / "ow" / "local"
    seed_dir.mkdir(parents=True)
    nested = seed_dir / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("nested")
    
    # Create workspace
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    
    ws = Workspace(
        root=ws_root,
        name="test",
        repos=(),
        settings={},
    )
    
    # Call the hook
    local.ow_workspace_created(ws)
    
    # Verify nested file was copied
    assert (ws_root / ".local" / "a" / "b" / "c" / "file.txt").read_text() == "nested"
