"""`ow repo rm` scenarios (§7): the worktree, the branch rule, the `[repos]` entry, the prompt.

The workspace is built with plain git, `write_workspace` and the index, like
`test_cli.py`: the removal commands read the same state however it came to be.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import repos_dir

REPO = "community"
BASE = "18.0"
BARE = "~/.local/share/ow/repos"


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; used where a failure is the expected state."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def _workspace(lab, *, branch: str | None = None, local_commit: bool = False) -> Path:
    """A one-repo workspace: a bare, a worktree on `branch` (or detached), a config, an index entry.

    `local_commit` adds a commit no remote has, so the worktree's branch is unpushed.
    """
    origin = lab.remote(REPO, {BASE: {"README.md": "c\n"}}, head=BASE)
    bare = repos_dir() / f"{REPO}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", str(origin))
    lab.git(bare, "fetch", "origin")

    root = lab.ws("voip")
    root.mkdir(parents=True, exist_ok=True)
    if branch is None:
        lab.git(bare, "worktree", "add", "--detach", str(root / REPO), f"origin/{BASE}")
    else:
        lab.git(bare, "worktree", "add", "-b", branch, str(root / REPO), f"origin/{BASE}")
        if local_commit:
            (root / REPO / "local.txt").write_text("x\n")
            lab.git(root / REPO, "add", "local.txt")
            lab.git(root / REPO, "commit", "-m", "local work")

    write_workspace(root / ".ow" / "config.toml", {REPO: f"origin/{BASE}"}, {}, declared={})
    index_add(root)
    return root


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
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}

    result = lab.ow("repo", "rm", REPO, "-y", cwd=root)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == plan
    assert not (root / REPO).exists()
    assert not _ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
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
    assert f"# {REPO}: kept unpushed branch dev" in lines
    assert lines[-1] == f"git -C {BARE}/{REPO}.git worktree remove ~/ws/voip/{REPO}"
    assert not (root / REPO).exists()
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/dev")


def test_the_prompt_is_asked_and_a_decline_removes_nothing(lab, monkeypatch):
    root = _workspace(lab, branch="18.0-voip")
    bare = repos_dir() / f"{REPO}.git"
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("repo", "rm", REPO, input="n\n", cwd=root)

    assert result.exit_code == 2
    assert result.stdout.count("Proceed?") == 1
    assert (root / REPO).is_dir()
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}


def test_rm_of_an_unknown_repo_is_a_usage_error(lab):
    root = _workspace(lab)

    result = lab.ow("repo", "rm", "nope", cwd=root)

    assert result.exit_code == 2
    assert f"error: unknown repo 'nope'; this workspace has: {REPO}" in result.stderr


def test_rm_never_forces_a_dirty_worktree_and_keeps_the_entry(lab):
    root = _workspace(lab, branch="18.0-voip")
    bare = repos_dir() / f"{REPO}.git"
    (root / REPO / "scratch.txt").write_text("x\n")

    result = lab.ow("repo", "rm", REPO, "-y", cwd=root)

    # Git refuses the untracked file, ow never adds --force, and the failed
    # batch leaves the `[repos]` entry and the branch alone.
    assert result.exit_code == 1
    shown = f"git -C {BARE}/{REPO}.git worktree remove ~/ws/voip/{REPO}"
    assert f"error: {REPO}: {shown}" in result.stderr
    assert "contains modified or untracked files" in result.stderr
    assert (root / REPO / "scratch.txt").is_file()
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert config["repos"] == {REPO: f"origin/{BASE}"}
