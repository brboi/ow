"""`ow prune` scenarios (§7): prunable worktrees, pushed vs kept orphan branches, dead index
entries — all under one prompt.

The workspace comes from `ow repo add` + `ow init` (§16); the orphans and the dead index
entry are then arranged by hand, the states prune is asked to clean up.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ow.core.index import add as index_add
from ow.core.paths import repos_dir

BARE = "~/.local/share/ow/repos"
REPO = "community"
BASE = "18.0"
HINT = "moved a workspace? run `ow init` there first"


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; used where a failure is the expected state."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def _index_lines() -> list[str]:
    """The workspace index file's entries."""
    index_file = Path(os.environ["XDG_STATE_HOME"]) / "ow" / "workspaces"
    if not index_file.exists():
        return []
    return [line for line in index_file.read_text().splitlines() if line]


def _machine(lab):
    """A machine with one bare: a prunable worktree on a pushed branch, a pushed orphan, an
    unpushed orphan, and a dead index entry."""
    remote = lab.remote(REPO, {BASE: {"README.md": "c\n"}}, head=BASE)
    assert lab.ow("repo", "add", REPO, f"file://{remote}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), REPO, "--base", BASE, "--branch", "18.0-voip").exit_code == 0
    bare = repos_dir() / f"{REPO}.git"

    # The worktree moves away: its registration becomes prunable, and its branch — whose
    # tip is the base's, so pushed — still counts as checked out (§7).
    (ws / REPO).rename(ws / f"{REPO}-moved")
    # A pushed orphan: created at a remote tip, checked out in no worktree.
    lab.git(bare, "branch", "pushed", f"origin/{BASE}")
    # An unpushed orphan: a commit no remote has, held only by the bare.
    tree = lab.sha(bare, f"origin/{BASE}^{{tree}}")
    sha = subprocess.run(
        ["git", "-C", str(bare), "commit-tree", tree, "-m", "local only"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    lab.git(bare, "branch", "kept", sha)
    # A dead index entry: a path that never held a workspace.
    index_add(lab.ws("gone"))
    return ws, bare


PLAN = [
    f"# ~/ws/voip/{REPO}: prunable ({HINT})",
    f"# {REPO}: kept unpushed branch kept",
    f"git -C {BARE}/{REPO}.git worktree prune",
    f"git -C {BARE}/{REPO}.git branch -D pushed",
    "ow forget ~/ws/gone",
]


def test_prune_cleans_the_whole_machine_with_one_prompt(lab):
    ws, bare = _machine(lab)
    plan = PLAN

    dry = lab.ow("prune", "-n")

    assert dry.exit_code == 0
    assert dry.stdout.splitlines() == plan
    # `-n` cleans nothing.
    assert f"worktree {ws / REPO}" in lab.git(bare, "worktree", "list", "--porcelain")
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/kept")
    assert str(lab.ws("gone")) in _index_lines()

    result = lab.ow("prune", "-y")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == plan
    assert f"worktree {ws / REPO}" not in lab.git(bare, "worktree", "list", "--porcelain")
    assert not _ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    # The branch of a prunable worktree still counts as checked out, pushed or not.
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/18.0-voip")
    # Unpushed orphans stay.
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/kept")
    assert str(lab.ws("gone")) not in _index_lines()
    assert str(ws) in _index_lines()


def test_a_declined_prompt_prunes_nothing(lab, monkeypatch):
    ws, bare = _machine(lab)
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("prune", input="n\n")

    assert result.exit_code == 2
    assert result.stdout.count("Proceed?") == 1
    assert f"worktree {ws / REPO}" in lab.git(bare, "worktree", "list", "--porcelain")
    assert _ok(bare, "rev-parse", "--verify", "refs/heads/pushed")
    assert str(lab.ws("gone")) in _index_lines()


def test_prune_on_a_clean_machine_prints_nothing_and_asks_nothing(lab):
    result = lab.ow("prune", "-y")

    assert result.exit_code == 0
    assert result.stdout == ""
