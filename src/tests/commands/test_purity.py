"""Read-only commands must be read-only, down to the last mtime.

`ow status`, `ow ls`, shell completion and every dry-run promise not to
write, and "not to write" has to mean the whole tree: the global config ow
used to bootstrap on first read, the discovery index it used to prune while
reading, and the worktree Git index a plain `git status` refreshes as a side
effect of *inspecting*. So each test snapshots every path, byte, mode and
mtime under an isolated XDG tree and the workspace, runs the command, and
compares the two snapshots — including the case where no global config and
no index file exist at all, which is exactly when the old code wrote them.

The harness lives here so Task 8 can extend the same proofs to `ow files`
and `ow render`'s inspection half.
"""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ow import __main__ as cli
from ow.commands.files import cmd_files
from ow.commands.ls import cmd_ls
from ow.commands.prune import cmd_prune
from ow.commands.pull import cmd_pull
from ow.commands.rebase import cmd_rebase
from ow.commands.reset import cmd_reset
from ow.commands.status import cmd_status
from ow.commands.switch import cmd_switch
from ow.utils import index, paths
from ow.utils.config import Config, WorkspaceConfig, parse_branch_spec, write_workspace_config
from ow.utils.refs import FetchOutcome
from ow.utils.resolver import resolve_workspace


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _bare_repo(tmp_path: Path, alias: str = "community") -> Path:
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "A")

    repos = paths.repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{alias}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        capture_output=True, text=True, check=True,
    )
    _git(bare, "config", "user.email", "t@t")
    _git(bare, "config", "user.name", "T")
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare


def _workspace(tmp_path: Path, *, spec: str = "master..featA") -> tuple[Config, Path, Path]:
    """A real workspace: one bare repo, one linked worktree, schema-2 config."""
    bare = _bare_repo(tmp_path)
    ws_dir = tmp_path / "workspaces" / "test"
    worktree = ws_dir / "community"
    ws_dir.mkdir(parents=True)
    _git(bare, "worktree", "add", "-q", str(worktree), "-b", "featA", "master")
    (worktree / "b.txt").write_text("b")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "B")
    # A stale stat cache is what makes `git status` want to refresh the
    # index: touching a tracked file without committing it is the ordinary
    # case that must not cost the user a repo write.
    # A stale stat cache is what makes `git status` want to refresh the
    # index: age a tracked file by a couple of seconds — no content change,
    # so nothing is reported dirty — and a probe that takes Git's optional
    # lock really would write. Waiting for the clock instead would cost
    # every test in this file a second of sleep for the same staleness.
    stale = worktree / "a.txt"
    stat = stale.stat()
    os.utime(stale, (stat.st_atime, stat.st_mtime + 2))

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec(spec)}),
    )
    return Config(remotes={}), ws_dir, worktree


def _fetched(tracks: dict[str, str], upstreams: dict[str, str] | None = None) -> FetchOutcome:
    return FetchOutcome(
        tracks=tracks,
        upstreams=upstreams or {},
        specs={alias: parse_branch_spec(ref) for alias, ref in tracks.items()},
        upstream_before={},
    )


def _nothing_to_pull(selected_ws, ws_dir, config, **kwargs) -> FetchOutcome:
    """A fetch that never happens: this is a file-purity test, not a network one."""
    return _fetched({alias: spec.base_ref for alias, spec in selected_ws.repos.items()})


# ---------------------------------------------------------------------------
# No global config, no index: the case that used to bootstrap both
# ---------------------------------------------------------------------------


def test_status_with_no_global_config_and_no_index_writes_nothing(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys, monkeypatch
) -> None:
    config, ws_dir, _ = _workspace(tmp_path)
    monkeypatch.chdir(xdg)
    assert not paths.config_file().exists()
    assert not paths.index_file().exists()
    before = tree_snapshot.capture(xdg, ws_dir)

    cmd_status(config, workspace=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_ls_with_no_index_writes_nothing(xdg: Path, tmp_path: Path, tree_snapshot, capsys) -> None:
    before = tree_snapshot.capture(xdg)

    cmd_ls()

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg)) == {}
    assert "No known workspaces" in capsys.readouterr().out


def test_ls_does_not_touch_a_stale_index(xdg: Path, tmp_path: Path, tree_snapshot, capsys) -> None:
    """`ow ls` used to prune while reading: a listing rewrote the file."""
    alive = tmp_path / "workspaces" / "alive"
    (alive / ".ow").mkdir(parents=True)
    (alive / ".ow" / "config.toml").write_text('version = 2\n[repos]\n')
    index.remember(alive)
    gone = tmp_path / "workspaces" / "gone"
    (gone / ".ow").mkdir(parents=True)
    index.remember(gone)
    shutil.rmtree(gone)
    before = tree_snapshot.capture(xdg)

    cmd_ls()

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg)) == {}
    assert "alive" in capsys.readouterr().out


def test_completion_writes_nothing(xdg: Path, tmp_path: Path, tree_snapshot) -> None:
    """Tab completion runs on every keystroke: it may never write a thing."""
    ws_dir = tmp_path / "workspaces" / "complete-me"
    (ws_dir / ".ow").mkdir(parents=True)
    (ws_dir / ".ow" / "config.toml").write_text('version = 2\n[repos]\n')
    index.remember(ws_dir)
    before = tree_snapshot.capture(xdg)

    names = cli.complete_workspace_name(None, "")

    assert names == ["complete-me"]
    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg)) == {}


def test_resolving_a_workspace_writes_nothing(xdg: Path, tmp_path: Path, tree_snapshot) -> None:
    """Resolution is the one call every command makes: it must not remember."""
    _, ws_dir, _ = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    resolve_workspace(name=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}


# ---------------------------------------------------------------------------
# Dry-runs and no-ops
# ---------------------------------------------------------------------------


def test_pull_dry_run_leaves_the_tree_and_the_git_index_alone(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    """The Git worktree index is part of the tree under test.

    `gather_pull_facts` asks git for the dirty files, and a plain
    `git status` takes the index lock and rewrites `.git/index` when it
    refreshes stat information. That write has to be off for a probe."""
    config, ws_dir, worktree = _workspace(tmp_path)
    # A linked worktree keeps its index in the bare repo, not under `.git/`:
    # the whole bare repo is inside the isolated XDG data directory, so the
    # snapshot below covers exactly the file a refreshing `git status` writes.
    git_index = Path(_git(worktree, "rev-parse", "--git-path", "index"))
    assert git_index.exists(), git_index
    before = tree_snapshot.capture(xdg, ws_dir)

    with patch("ow.commands.pull.fetch_workspace_refs", side_effect=_nothing_to_pull):
        cmd_pull(config, workspace=str(ws_dir), dry_run=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_rebase_dry_run_leaves_the_tree_alone(xdg: Path, tmp_path: Path, tree_snapshot, capsys) -> None:
    config, ws_dir, _ = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    with patch("ow.commands.rebase.fetch_workspace_refs", side_effect=_nothing_to_pull):
        cmd_rebase(config, workspace=str(ws_dir), dry_run=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_reset_dry_run_leaves_the_tree_alone(xdg: Path, tmp_path: Path, tree_snapshot, capsys) -> None:
    config, ws_dir, _ = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    with patch("ow.commands.reset.fetch_workspace_refs", side_effect=_nothing_to_pull):
        cmd_reset(config, workspace=str(ws_dir), dry_run=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_switch_dry_run_leaves_the_tree_alone(xdg: Path, tmp_path: Path, tree_snapshot, capsys) -> None:
    config, ws_dir, _ = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    cmd_switch(config, "featA", workspace=str(ws_dir), dry_run=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_prune_dry_run_leaves_the_index_and_backups_alone(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    """A declined confirmation must be as harmless as --dry-run."""
    gone = tmp_path / "workspaces" / "gone"
    (gone / ".ow").mkdir(parents=True)
    index.remember(gone)
    shutil.rmtree(gone)
    before = tree_snapshot.capture(xdg)

    cmd_prune(dry_run=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg)) == {}
    capsys.readouterr()


def test_prune_declined_writes_nothing(xdg: Path, tmp_path: Path, tree_snapshot, capsys, monkeypatch) -> None:
    """Answering no leaves the index exactly as it was, dead entries included.

    The prompt comes before the cleanup, not after it: a user who declines a
    branch deletion has not agreed to any other write either."""
    bare = _bare_repo(tmp_path)
    _git(bare, "branch", "spent", "refs/remotes/origin/master")
    gone = tmp_path / "workspaces" / "gone"
    (gone / ".ow").mkdir(parents=True)
    index.remember(gone)
    shutil.rmtree(gone)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    before = tree_snapshot.capture(xdg)

    with pytest.raises(SystemExit) as exc:
        cmd_prune()

    assert exc.value.code == 2
    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg)) == {}
    assert "spent" in _git(bare, "branch", "--list", "spent")
    capsys.readouterr()


def test_prune_with_nothing_to_do_writes_nothing(xdg: Path, tmp_path: Path, tree_snapshot, capsys) -> None:
    alive = tmp_path / "workspaces" / "alive"
    (alive / ".ow").mkdir(parents=True)
    (alive / ".ow" / "config.toml").write_text("version = 2\n[repos]\n")
    index.remember(alive)
    before = tree_snapshot.capture(xdg)

    cmd_prune()

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg)) == {}
    capsys.readouterr()


def test_a_declared_but_absent_worktree_is_a_read_not_a_repair(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    """`ow status` on a workspace that lost a worktree must not rebuild it."""
    ws_dir = tmp_path / "workspaces" / "hollow"
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec("master")}),
    )
    before = tree_snapshot.capture(xdg, ws_dir)

    cmd_status(Config(remotes={}), workspace=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


# ---------------------------------------------------------------------------
# `ow files` is read-only: neither listing nor diffing may write a byte,
# including the bootstrap-on-first-read and index-pruning cases above.
# ---------------------------------------------------------------------------


def test_files_with_no_global_config_and_no_index_writes_nothing(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    config, ws_dir, _ = _workspace(tmp_path)
    assert not paths.config_file().exists()
    assert not paths.index_file().exists()
    before = tree_snapshot.capture(xdg, ws_dir)

    cmd_files(config, workspace=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_files_diff_with_no_global_config_and_no_index_writes_nothing(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    config, ws_dir, _ = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    with pytest.raises(SystemExit):
        cmd_files(config, workspace=str(ws_dir), show_diff=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_files_on_a_workspace_missing_a_worktree_writes_nothing(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    """A blocked plan (repair guidance, no generation) is still read-only."""
    ws_dir = tmp_path / "workspaces" / "hollow"
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec("master")}),
    )
    before = tree_snapshot.capture(xdg, ws_dir)

    with pytest.raises(SystemExit):
        cmd_files(Config(remotes={}), workspace=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()


def test_files_leaves_the_git_index_alone(
    xdg: Path, tmp_path: Path, tree_snapshot, capsys
) -> None:
    """Addon-path scanning and the Git worktree it walks must not be touched."""
    config, ws_dir, worktree = _workspace(tmp_path)
    git_index = Path(_git(worktree, "rev-parse", "--git-path", "index"))
    assert git_index.exists(), git_index
    before = tree_snapshot.capture(xdg, ws_dir)

    cmd_files(config, workspace=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}
    capsys.readouterr()