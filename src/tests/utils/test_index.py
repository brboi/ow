"""Tests for ow.utils.index.

The index is a hint, not a database: the only truth about a workspace is its
.ow/config.toml on disk. These tests prove the properties that keep it that
way — pruning on read, deduplication, atomic writes, and (crucially) that a
read which prunes nothing does not touch the file.
"""

from pathlib import Path

import pytest

from ow.utils import index, paths


def _make_workspace(base: Path, name: str) -> Path:
    ws = base / name
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")
    return ws


def test_missing_index_returns_empty_list(xdg: Path) -> None:
    assert index.known_workspaces() == []


def test_remember_creates_file_and_parent_dir(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    assert not paths.index_file().parent.exists()

    index.remember(ws)

    assert paths.index_file().exists()
    assert index.known_workspaces() == [ws.resolve()]


def test_remember_same_path_twice_writes_one_line(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")

    index.remember(ws)
    index.remember(ws)

    lines = paths.index_file().read_text().splitlines()
    assert lines == [str(ws.resolve())]


def test_remember_resolves_relative_path(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _make_workspace(xdg, "alpha")
    monkeypatch.chdir(xdg)

    index.remember(Path("alpha"))

    assert index.known_workspaces() == [ws.resolve()]


def test_known_workspaces_prunes_vanished_entries_and_rewrites(xdg: Path) -> None:
    alive = _make_workspace(xdg, "alive")
    gone = _make_workspace(xdg, "gone")
    index.remember(alive)
    index.remember(gone)

    # The workspace disappears without going through forget().
    (gone / ".ow" / "config.toml").unlink()

    result = index.known_workspaces()

    assert result == [alive.resolve()]
    assert paths.index_file().read_text().splitlines() == [str(alive.resolve())]


def test_known_workspaces_does_not_rewrite_when_nothing_pruned(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    index.remember(ws)

    # os.replace() consults the *directory's* write permission, not the
    # target file's mode bits — a read-only file would not stop a write.
    # Making the directory read-only is what actually forces a write to
    # raise, which is the only way this test can distinguish "no rewrite
    # attempted" from "rewrite attempted and happened to produce the same
    # bytes".
    index_dir = paths.index_file().parent
    index_dir.chmod(0o555)
    try:
        result = index.known_workspaces()
    finally:
        index_dir.chmod(0o755)  # so tmp_path cleanup can remove it

    assert result == [ws.resolve()]


def test_known_workspaces_does_not_rewrite_when_nothing_pruned_mtime(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    index.remember(ws)

    before = paths.index_file().stat().st_mtime_ns

    index.known_workspaces()

    after = paths.index_file().stat().st_mtime_ns
    assert after == before


def test_known_workspaces_dedupes_and_rewrites(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{ws.resolve()}\n{ws.resolve()}\n")

    result = index.known_workspaces()

    assert result == [ws.resolve()]
    assert target.read_text().splitlines() == [str(ws.resolve())]


def test_find_by_name(xdg: Path) -> None:
    canary = _make_workspace(xdg, "canary")
    other = _make_workspace(xdg, "other")
    index.remember(canary)
    index.remember(other)

    assert index.find_by_name("canary") == [canary.resolve()]
    assert index.find_by_name("missing") == []


def test_find_by_name_multiple_matches(xdg: Path) -> None:
    first = _make_workspace(xdg, "dupe")
    second_dir = xdg / "nested"
    second_dir.mkdir()
    second = _make_workspace(second_dir, "dupe")
    index.remember(first)
    index.remember(second)

    result = index.find_by_name("dupe")

    assert sorted(result) == sorted([first.resolve(), second.resolve()])


def test_forget_removes_entry_and_keeps_others(xdg: Path) -> None:
    alpha = _make_workspace(xdg, "alpha")
    beta = _make_workspace(xdg, "beta")
    index.remember(alpha)
    index.remember(beta)

    index.forget(alpha)

    assert index.known_workspaces() == [beta.resolve()]


def test_blank_and_whitespace_lines_are_ignored(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"\n   \n{ws.resolve()}\n\t\n")

    assert index.known_workspaces() == [ws.resolve()]
