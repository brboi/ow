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


def test_known_workspaces_filters_vanished_entries_without_rewriting(xdg: Path) -> None:
    """A read reports what is usable and leaves the file exactly as it was.

    Pruning used to happen on read, which made `ow status`, a completion
    callback and a dry-run mutate the user's state directory. Dropping the
    entry for good is `ow prune`'s explicit job now, and the raw file still
    says what it said."""
    alive = _make_workspace(xdg, "alive")
    gone = _make_workspace(xdg, "gone")
    index.remember(alive)
    index.remember(gone)
    before = paths.index_file().read_text()

    # The workspace disappears behind the index's back.
    (gone / ".ow" / "config.toml").unlink()

    result = index.known_workspaces()

    assert result == [alive.resolve()]
    assert paths.index_file().read_text() == before
    assert index.list_workspaces() == [alive.resolve(), gone.resolve()]


def test_prune_drops_exactly_the_named_entries(xdg: Path) -> None:
    """`ow prune` names what died; everything else in the file survives."""
    alive = _make_workspace(xdg, "alive")
    gone = _make_workspace(xdg, "gone")
    index.remember(alive)
    index.remember(gone)
    (gone / ".ow" / "config.toml").unlink()

    index.prune([gone.resolve()])

    assert paths.index_file().read_text().splitlines() == [str(alive.resolve())]
    assert index.known_workspaces() == [alive.resolve()]


def test_prune_is_a_noop_for_an_empty_set(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    index.remember(ws)
    before = paths.index_file().stat().st_mtime_ns

    index.prune([])

    assert paths.index_file().stat().st_mtime_ns == before


def test_prune_keeps_an_entry_written_after_it_was_read(xdg: Path) -> None:
    """Re-reading under the lock is what saves a concurrent remember().

    The cleanup decides what is dead from a list it read earlier; by the
    time it writes, another `ow init` may have recorded a workspace. Only
    the named entries may go."""
    alive = _make_workspace(xdg, "alive")
    gone = _make_workspace(xdg, "gone")
    newcomer = _make_workspace(xdg, "newcomer")
    index.remember(alive)
    index.remember(gone)

    doomed = [gone.resolve()]
    index.remember(newcomer)
    index.prune(doomed)

    assert set(index.known_workspaces()) == {alive.resolve(), newcomer.resolve()}


def test_forget_removes_one_entry_and_is_a_noop_when_absent(xdg: Path) -> None:
    first = _make_workspace(xdg, "first")
    second = _make_workspace(xdg, "second")
    index.remember(first)
    index.remember(second)

    index.forget(first)

    assert index.known_workspaces() == [second.resolve()]
    index.forget(xdg / "never-remembered")
    assert index.known_workspaces() == [second.resolve()]


def test_known_workspaces_never_writes_even_when_it_could(xdg: Path) -> None:
    """The strongest form: a read-only state directory changes nothing.

    A vanished entry is exactly the case that used to trigger a rewrite, so
    making the directory unwritable is what tells "no rewrite attempted"
    from "rewrite attempted and happened to produce the same bytes"."""
    ws = _make_workspace(xdg, "alpha")
    index.remember(ws)
    (ws / ".ow" / "config.toml").unlink()

    index_dir = paths.index_file().parent
    index_dir.chmod(0o555)
    try:
        result = index.known_workspaces()
    finally:
        index_dir.chmod(0o755)  # so tmp_path cleanup can remove it

    assert result == []


def test_known_workspaces_does_not_rewrite_when_nothing_pruned_mtime(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    index.remember(ws)
    (ws / ".ow" / "config.toml").unlink()

    before = paths.index_file().stat().st_mtime_ns

    index.known_workspaces()

    after = paths.index_file().stat().st_mtime_ns
    assert after == before


def test_known_workspaces_dedupes_without_rewriting(xdg: Path) -> None:
    ws = _make_workspace(xdg, "alpha")
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{ws.resolve()}\n{ws.resolve()}\n")
    before = target.read_text()

    result = index.known_workspaces()

    assert result == [ws.resolve()]
    assert target.read_text() == before


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


def test_blank_lines_are_ignored_on_read(xdg: Path) -> None:
    """A blank line is as much rubbish as a dead entry — and like one, it is
    filtered out of the result rather than erased from a file this call has
    no business writing. `ow prune` and the next `remember` are what clean it."""
    ws = _make_workspace(xdg, "alpha")
    target = paths.index_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"\n   \n{ws.resolve()}\n\t\n")

    assert index.known_workspaces() == [ws.resolve()]
    assert target.read_text() == f"\n   \n{ws.resolve()}\n\t\n"


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




def test_stale_lock_is_broken_and_unlinked(xdg: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed writer leaves a lockfile behind.  The next call must not
    wait the full timeout every time — it must break the lock, warn on
    stderr, and proceed.
    """
    ws = _make_workspace(xdg, "alpha")
    lock = paths.index_file().with_name("workspaces.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")

    monkeypatch.setattr(index, "_LOCK_TIMEOUT", 0.1)
    monkeypatch.setattr(index, "_LOCK_RETRY", 0.001)

    index.remember(ws)

    assert not lock.exists()
    captured = capsys.readouterr()
    assert "stale" in captured.err.lower() or "lock" in captured.err.lower()
    assert paths.index_file().read_text().splitlines() == [str(ws.resolve())]




def test_remember_degrades_on_readonly_state_dir(xdg: Path, capsys: pytest.CaptureFixture) -> None:
    """A read-only state directory must not abort commands that only cache."""
    ws = _make_workspace(xdg, "alpha")
    state = xdg / "state"
    state.chmod(0o500)
    try:
        index.remember(ws)
    finally:
        state.chmod(0o755)
    captured = capsys.readouterr()
    assert "index" in captured.err.lower() or "cache" in captured.err.lower() or "cannot write" in captured.err.lower()


def test_known_workspaces_reads_with_a_readonly_state_dir(xdg: Path) -> None:
    """A read needs no write access at all — not even for a prunable entry."""
    ws = _make_workspace(xdg, "alpha")
    index.remember(ws)
    # The condition that used to force a rewrite: a duplicate line.
    target = paths.index_file()
    target.write_text(target.read_text() + str(ws.resolve()) + "\n")
    state = xdg / "state"
    state.chmod(0o500)
    try:
        result = index.known_workspaces()
    finally:
        state.chmod(0o755)
    assert result == [ws.resolve()]


def test_concurrent_remembers_do_not_lose_entries(xdg: Path) -> None:
    """`ow init` in one terminal must not erase what another just wrote.

    remember() is a read-modify-write, so two concurrent lifecycles can
    clobber each other's entry. The file is a hint and it self-heals, but
    silently dropping a workspace someone just created is a hint that lies,
    and the fix is a lockfile beside the temp file _write already makes.
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
