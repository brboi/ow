"""Tests for ow.utils.index.

The index is a hint, not a database: the only truth about a workspace is its
.ow/config.toml on disk. These tests prove the properties that keep it that
way — pruning on read, deduplication, atomic writes, and (crucially) that a
read which prunes nothing does not touch the file.
"""

import os
import time
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

    # The workspace disappears behind the index's back.
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


def test_blank_and_whitespace_lines_are_ignored(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"\n   \n{ws.resolve()}\n\t\n")

    assert index.known_workspaces() == [ws.resolve()]


def test_an_unstattable_entry_is_kept_not_pruned(xdg: Path) -> None:
    """A stat error is ignorance, not absence.

    An indexed workspace can sit under a directory the user cannot stat
    right now — a mount that went away, a parent someone chmod'd, a stale
    NFS handle. Path.exists() propagates those rather than returning False,
    so an unguarded read takes down `ow ls`, `ow prune` and every name
    lookup with a traceback, and the designated cleanup command dies on the
    same line. Neither may happen: the entry survives, because the index
    cannot tell a dead workspace from an unreachable one.
    """
    reachable = _make_workspace(xdg, "reachable")
    vault = xdg / "vault"
    vault.mkdir()
    hidden = _make_workspace(vault, "hidden")
    index.remember(reachable)
    index.remember(hidden)

    vault.chmod(0o000)
    try:
        result = index.known_workspaces()
    finally:
        vault.chmod(0o755)  # so tmp_path cleanup can remove it

    assert sorted(result) == sorted([reachable.resolve(), hidden.resolve()])
    assert str(hidden.resolve()) in paths.index_file().read_text()


def test_an_entry_whose_stat_errors_without_permissions_is_kept(xdg: Path) -> None:
    """The same guarantee, provable as root.

    The permission test above is a no-op for uid 0, which stats anything.
    ENAMETOOLONG is refused by the kernel for everyone, so it pins the
    behaviour down on any machine.
    """
    alive = _make_workspace(xdg, "alive")
    index.remember(alive)
    too_long = xdg / ("n" * 300) / "ws"
    target = paths.index_file()
    target.write_text(f"{alive.resolve()}\n{too_long}\n")

    assert index.known_workspaces() == [alive.resolve(), too_long]
    assert str(too_long) in target.read_text()


def test_find_by_name_is_exact_not_a_prefix(xdg: Path) -> None:
    """A name is a whole name. The other fixtures here — canary, other,
    dupe — are not substrings of one another, so a substring match passed
    every one of them. This is what the resolver's name branch feeds, and
    it feeds `ow rebase`: `ow rebase my` quietly acting on `my-workspace`
    is a destructive command aimed at the wrong tree."""
    index.remember(_make_workspace(xdg, "my-workspace"))

    assert index.find_by_name("my") == []
    assert index.find_by_name("workspace") == []
    assert index.find_by_name("my-workspace") == [(xdg / "my-workspace").resolve()]


def test_blank_lines_are_cleaned_out_of_the_file(xdg: Path) -> None:
    """Ignoring a blank line on read is not enough — a read that leaves it
    behind re-reads it forever. Pruning on read is how this file stays
    honest, and a blank line is as much rubbish as a dead entry."""
    ws = _make_workspace(xdg, "alpha")
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"\n   \n{ws.resolve()}\n\t\n")

    assert index.known_workspaces() == [ws.resolve()]
    assert target.read_text() == f"{ws.resolve()}\n"


def test_remember_appends_keeping_insertion_order(xdg: Path) -> None:
    """`ow ls` prints the index in file order, so the order is user-visible:
    workspaces appear in the order they were first seen. Prepending would
    reverse that listing without a single test noticing."""
    first = _make_workspace(xdg, "first")
    second = _make_workspace(xdg, "second")
    third = _make_workspace(xdg, "third")

    for ws in (first, second, third):
        index.remember(ws)

    assert index.known_workspaces() == [first.resolve(), second.resolve(), third.resolve()]


def test_concurrent_remembers_do_not_lose_entries(xdg: Path) -> None:
    """`ow init` in one terminal must not erase what another just wrote.

    remember() is a read-modify-write, and known_workspaces() rewrites on
    *read* too — so a tab-completion or an `ow ls` can clobber a concurrent
    `ow init`. The file is a hint and it self-heals, but silently dropping
    a workspace someone just created is a hint that lies, and the fix is a
    lockfile beside the temp file _write already makes.
    """
    workers, per_worker = 8, 8
    plots = [
        [_make_workspace(xdg, f"ws-{w}-{i}") for i in range(per_worker)]
        for w in range(workers)
    ]

    # Fork rather than thread: two `ow` invocations are two processes, and a
    # lock that only excluded threads would prove nothing about them.
    go = xdg / "go"
    pids = []
    for row in plots:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never reports coverage
            try:
                while not go.exists():
                    time.sleep(0.001)
                for ws in row:
                    index.remember(ws)
            finally:
                os._exit(0)
        pids.append(pid)
    go.write_text("")
    for pid in pids:
        os.waitpid(pid, 0)

    expected = {ws.resolve() for row in plots for ws in row}
    assert set(index.known_workspaces()) == expected
