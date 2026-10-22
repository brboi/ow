"""`ow repo rm` scenarios (§7): the worktree, the branch rule, the `[repos]` entry, the prompt.

The workspace is built with plain git, `write_workspace` and the index, like
`test_cli.py`: the removal commands read the same state however it came to be.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import git_ok, workspace

REPO = "community"
BASE = "18.0"
BARE = "~/.local/share/ow/repos"


def _workspace(lab, *, branch: str | None = None, local_commit: bool = False) -> Path:
    """A one-repo workspace: a bare, a worktree on `branch` (or detached), a config, an index entry.

    `local_commit` adds a commit no remote has, so the worktree's branch is unpushed.
    """
    return workspace(lab, branch=branch, local_commit=local_commit)


def test_rm_drops_the_worktree_a_pushed_branch_and_the_repos_entry(lab):
    root = _workspace(lab, branch="18.0-voip")
    bare = repos_dir() / f"{REPO}.git"
    plan = [
        f"git -C {BARE}/{REPO}.git worktree remove ~/ws/voip/{REPO}",
        f"git -C {BARE}/{REPO}.git branch -D 18.0-voip",
    ]

    dry = lab.ow("repo", "rm", REPO, "-n", cwd=root)

    assert dry.exit_code == 0
    assert dry.stdout.splitlines() == plan
    # `-n` changes nothing: worktree, branch and entry all stay.
    assert (root / REPO).is_dir()
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}

    result = lab.ow("repo", "rm", REPO, "-y", cwd=root)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == plan
    assert not (root / REPO).exists()
    assert not git_ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    listing = lab.git(bare, "worktree", "list", "--porcelain")
    assert f"worktree {root / REPO}" not in listing
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config.get("repos", {}) == {}


def test_rm_keeps_and_lists_an_unpushed_branch(lab):
    root = _workspace(lab, branch="dev", local_commit=True)
    bare = repos_dir() / f"{REPO}.git"

    result = lab.ow("repo", "rm", REPO, "-y", cwd=root)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert (
        f"# {REPO}: kept unpushed branch dev; delete it with "
        f"`git -C {BARE}/{REPO}.git branch -D dev`"
    ) in lines
    assert lines[-1] == f"git -C {BARE}/{REPO}.git worktree remove ~/ws/voip/{REPO}"
    assert not (root / REPO).exists()
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/dev")


def test_the_prompt_is_asked_and_a_decline_removes_nothing(lab, monkeypatch):
    root = _workspace(lab, branch="18.0-voip")
    bare = repos_dir() / f"{REPO}.git"
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("repo", "rm", REPO, input="n\n", cwd=root)

    assert result.exit_code == 2
    assert result.stdout.count("Proceed?") == 1
    assert (root / REPO).is_dir()
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}


def test_rm_of_an_unknown_repo_is_a_usage_error(lab):
    root = _workspace(lab)

    result = lab.ow("repo", "rm", "nope", cwd=root)

    assert result.exit_code == 2
    assert f"error: unknown repo 'nope'; this workspace has: {REPO}" in result.stderr


def test_a_dirty_worktree_refuses_before_the_prompt(lab):
    root = _workspace(lab, branch="18.0-voip")
    bare = repos_dir() / f"{REPO}.git"
    (root / REPO / "README.md").write_text("changed\n")
    (root / REPO / "scratch.txt").write_text("x\n")

    result = lab.ow("repo", "rm", REPO, cwd=root)

    # Refused before the prompt: no `-y`, no terminal, and still no usage error.
    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert f"# {REPO}: modified README.md" in lines
    assert f"# {REPO}: untracked scratch.txt" in lines
    assert f"# {REPO}: commit or stash these changes first" in lines
    assert not [line for line in lines if line.startswith("git ")]
    assert (root / REPO / "scratch.txt").is_file()
    assert git_ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}


def test_a_detached_worktree_with_local_commits_refuses(lab):
    root = _workspace(lab)
    bare = repos_dir() / f"{REPO}.git"
    worktree = root / REPO
    (worktree / "local.txt").write_text("x\n")
    lab.git(worktree, "add", "local.txt")
    lab.git(worktree, "commit", "-m", "local work")
    sha = lab.sha(worktree, "HEAD")

    result = lab.ow("repo", "rm", REPO, "-y", cwd=root)

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert any(f"# {REPO}: detached HEAD has 1 commit(s) no remote has" in line for line in lines)
    assert any("switch -c <branch>" in line for line in lines)
    assert worktree.is_dir()
    assert lab.sha(worktree, "HEAD") == sha
    assert git_ok(bare, "rev-parse", "--verify", f"refs/remotes/origin/{BASE}")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}


def test_a_repos_entry_with_a_missing_worktree_can_be_removed(lab):
    root = _workspace(lab, branch="18.0-voip")
    shutil.rmtree(root / REPO)

    result = lab.ow("repo", "rm", REPO, "-y", cwd=root)

    assert result.exit_code == 0
    assert f"# {REPO}: worktree already gone" in result.stdout.splitlines()
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config.get("repos", {}) == {}
