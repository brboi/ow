"""`ow fetch` against real git repositories.

What it reports — how many commits arrived, and whether a ref was
force-pushed under you — is a comparison of the ref before and after the
fetch, which only exists while a real fetch is happening.
"""

import subprocess
from pathlib import Path

import pytest

from ow.commands.fetch import cmd_fetch
from ow.utils import paths
from ow.utils.config import (
    Config,
    RemoteConfig,
    WorkspaceConfig,
    parse_branch_spec,
    write_workspace_config,
)
from ow.utils.git import rev_parse


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path, alias: str) -> tuple[Path, Path]:
    """A bare repo plus its 'origin' source, wired the way `ow apply` leaves one."""
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
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare, src


def _workspace(bare: Path, tmp_path: Path, alias: str, spec: str = "master") -> Path:
    ws_dir = tmp_path / "workspaces" / "test"
    branch = parse_branch_spec(spec).local_branch
    if branch:
        _git(bare, "worktree", "add", "-q", str(ws_dir / alias), "-b", branch, "master")
    else:
        _git(bare, "worktree", "add", "-q", "--detach", str(ws_dir / alias), "master")
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={alias: parse_branch_spec(spec)}),
    )
    return ws_dir


def _config(alias: str, src: Path) -> Config:
    return Config(remotes={alias: {"origin": RemoteConfig(url=str(src))}})


def test_commits_that_arrived_are_counted_and_no_worktree_moves(tmp_path, capsys, xdg):
    bare, src = _make_repo(tmp_path, "community")
    ws_dir = _workspace(bare, tmp_path, "community")
    worktree = ws_dir / "community"
    head_before = _git(worktree, "rev-parse", "HEAD")

    _git(src, "commit", "-q", "--allow-empty", "-m", "B")
    _git(src, "commit", "-q", "--allow-empty", "-m", "C")

    cmd_fetch(_config("community", src), workspace=str(ws_dir))

    assert rev_parse(bare, "refs/remotes/origin/master") == _git(src, "rev-parse", "master")
    # A fetch writes to the bare repo and to nothing else.
    assert _git(worktree, "rev-parse", "HEAD") == head_before
    out = capsys.readouterr().out
    assert "origin/master" in out
    assert "+2" in out


def test_a_force_pushed_ref_is_named_rather_than_counted_as_progress(tmp_path, capsys, xdg):
    """A rewritten upstream is not "n new commits": it no longer contains what
    it did, which is what `ow rebase` has to replay around."""
    bare, src = _make_repo(tmp_path, "community")
    _git(src, "branch", "feature", "master")
    _git(bare, "fetch", "-q", "origin", "+feature:refs/remotes/origin/feature")
    ws_dir = _workspace(bare, tmp_path, "community", "master..feature")
    _git(ws_dir / "community", "branch", "--set-upstream-to", "origin/feature")

    _git(src, "checkout", "-q", "feature")
    _git(src, "commit", "-q", "--allow-empty", "-m", "rewritten")

    cmd_fetch(_config("community", src), workspace=str(ws_dir))

    out = capsys.readouterr().out
    assert "force-pushed" not in out  # fast-forward so far

    _git(src, "commit", "-q", "--amend", "--allow-empty", "-m", "amended again")

    cmd_fetch(_config("community", src), workspace=str(ws_dir))

    out = capsys.readouterr().out
    assert "force-pushed" in out


def test_a_repo_whose_remote_is_unreachable_exits_non_zero(tmp_path, capsys, xdg):
    bare, src = _make_repo(tmp_path, "community")
    ws_dir = _workspace(bare, tmp_path, "community")
    _git(bare, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    with pytest.raises(SystemExit) as exit_info:
        cmd_fetch(_config("community", tmp_path / "gone.git"), workspace=str(ws_dir))

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "fetch failed" in captured.out
    assert "could not be fetched" in captured.err
