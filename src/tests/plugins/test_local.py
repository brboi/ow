"""Tests for the local plugin."""

from __future__ import annotations

import os

import pytest

from ow.core.workspace import Workspace
from ow.plugins import local


def _ws(root):
    return Workspace(root=root, name="test", repos=(), settings={})


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    seed = tmp_path / "config" / "ow" / "local"
    seed.mkdir(parents=True)
    return seed


def test_local_copies_missing_files(tmp_path, monkeypatch):
    seed = _seed(tmp_path, monkeypatch)
    (seed / "file1.txt").write_text("content1")
    (seed / "subdir").mkdir()
    (seed / "subdir" / "file2.txt").write_text("content2")

    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    local.ow_workspace_created(_ws(ws_root))

    assert (ws_root / ".local" / "file1.txt").read_text() == "content1"
    assert (ws_root / ".local" / "subdir" / "file2.txt").read_text() == "content2"


def test_local_never_overwrites(tmp_path, monkeypatch):
    seed = _seed(tmp_path, monkeypatch)
    (seed / "file1.txt").write_text("seed content")

    ws_root = tmp_path / "ws"
    local_dir = ws_root / ".local"
    local_dir.mkdir(parents=True)
    (local_dir / "file1.txt").write_text("existing content")

    local.ow_workspace_created(_ws(ws_root))

    assert (local_dir / "file1.txt").read_text() == "existing content"


def test_local_handles_missing_seed_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    local.ow_workspace_created(_ws(ws_root))

    assert not (ws_root / ".local").exists()


def test_local_creates_directories(tmp_path, monkeypatch):
    seed = _seed(tmp_path, monkeypatch)
    nested = seed / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("nested")

    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    local.ow_workspace_created(_ws(ws_root))

    assert (ws_root / ".local" / "a" / "b" / "c" / "file.txt").read_text() == "nested"


def test_local_preserves_seed_mode(tmp_path, monkeypatch):
    seed = _seed(tmp_path, monkeypatch)
    sourced = seed / "script.sh"
    sourced.write_text("#!/bin/sh\n")
    sourced.chmod(0o755)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    local.ow_workspace_created(_ws(ws_root))

    assert (ws_root / ".local" / "script.sh").stat().st_mode & 0o777 == 0o755


def test_local_skips_dangling_symlink_destination(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path, monkeypatch)
    (seed / "victim.txt").write_text("SEED")

    ws_root = tmp_path / "ws"
    local_dir = ws_root / ".local"
    local_dir.mkdir(parents=True)
    outside = tmp_path / "outside" / "victim.txt"
    (local_dir / "victim.txt").symlink_to(outside)

    local.ow_workspace_created(_ws(ws_root))

    assert (local_dir / "victim.txt").is_symlink()
    assert not outside.exists()
    assert "symlink" in capsys.readouterr().err


def test_local_skips_symlinked_local_destination(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path, monkeypatch)
    (seed / "file.txt").write_text("SEED")

    target = tmp_path / "outside" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("untouched")
    local_dir = tmp_path / "ws" / ".local"
    local_dir.mkdir(parents=True)
    (local_dir / "file.txt").symlink_to(target)

    local.ow_workspace_created(_ws(tmp_path / "ws"))

    assert target.read_text() == "untouched"
    assert "symlink" in capsys.readouterr().err


def test_local_skips_symlinked_local_dir(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path, monkeypatch)
    (seed / "inner.txt").write_text("SEED")

    outside = tmp_path / "outside"
    outside.mkdir()
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    (ws_root / ".local").symlink_to(outside)

    local.ow_workspace_created(_ws(ws_root))

    assert not (outside / "inner.txt").exists()
    assert "symlink" in capsys.readouterr().err


def test_local_skips_symlinked_local_subdir(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path, monkeypatch)
    (seed / "subdir").mkdir()
    (seed / "subdir" / "inner.txt").write_text("SEED")

    outside = tmp_path / "outside"
    outside.mkdir()
    ws_root = tmp_path / "ws"
    local_dir = ws_root / ".local"
    local_dir.mkdir(parents=True)
    (local_dir / "subdir").symlink_to(outside)

    local.ow_workspace_created(_ws(ws_root))

    assert not (outside / "inner.txt").exists()
    assert "symlink" in capsys.readouterr().err


def test_local_skips_symlink_seed_source(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path, monkeypatch)
    real = tmp_path / "real.txt"
    real.write_text("real content")
    (seed / "link.txt").symlink_to(real)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    local.ow_workspace_created(_ws(ws_root))

    assert not (ws_root / ".local" / "link.txt").exists()
    assert "symlink" in capsys.readouterr().err


def test_local_warns_on_unreadable_seed_file(tmp_path, monkeypatch, capsys):
    seed = _seed(tmp_path, monkeypatch)
    unreadable = seed / "unreadable.txt"
    unreadable.write_text("content")
    unreadable.chmod(0o000)
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("running as root: file permissions do not bite")

        ws_root = tmp_path / "ws"
        ws_root.mkdir()

        local.ow_workspace_created(_ws(ws_root))

        assert str(unreadable) in capsys.readouterr().err
        assert not (ws_root / ".local" / "unreadable.txt").exists()
    finally:
        unreadable.chmod(0o644)
