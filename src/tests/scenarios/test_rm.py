"""`ow rm <ws>` scenarios (§7): pushed vs kept branches, the all-or-nothing pre-flight.

The workspace is built with plain git, `write_workspace` and the index, like
`test_cli.py`: `rm` reads the same state however it came to be.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import repos_dir

BARE = "~/.local/share/ow/repos"
REPOS = (("community", "b-pushed"), ("enterprise", "b-kept"))
BASE = "18.0"


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; used where a failure is the expected state."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def _index_lines() -> list[str]:
    """The workspace index file's entries."""
    index_file = Path(os.environ["XDG_STATE_HOME"]) / "ow" / "workspaces"
    if not index_file.exists():
        return []
    return [line for line in index_file.read_text().splitlines() if line]


def _workspace(lab) -> Path:
    """A two-repo workspace: community on a pushed branch, enterprise on an unpushed one."""
    root = lab.ws("voip")
    root.mkdir(parents=True, exist_ok=True)
    for name, branch in REPOS:
        origin = lab.remote(name, {BASE: {"README.md": f"{name}\n"}}, head=BASE)
        bare = repos_dir() / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        lab.git(bare.parent, "init", "--bare", str(bare))
        lab.git(bare, "remote", "add", "origin", str(origin))
        lab.git(bare, "fetch", "origin")
        lab.git(bare, "worktree", "add", "-b", branch, str(root / name), f"origin/{BASE}")
        if branch == "b-kept":
            (root / name / "local.txt").write_text("x\n")
            lab.git(root / name, "add", "local.txt")
            lab.git(root / name, "commit", "-m", "local work")
    write_workspace(
        root / ".ow" / "config.toml",
        {repo: f"origin/{BASE}" for repo, _ in REPOS},
        {},
        declared={},
    )
    index_add(root)
    return root


def _plan(outside: int) -> list[str]:
    """The whole plan `ow rm voip` prints: the count, the notes, the steps, the after steps."""
    lines = [f"# {outside} file(s) outside the repos", "# enterprise: kept unpushed branch b-kept"]
    for name, branch in REPOS:
        lines.append(f"git -C {BARE}/{name}.git worktree remove ~/ws/voip/{name}")
        if branch == "b-pushed":
            lines.append(f"git -C {BARE}/{name}.git branch -D b-pushed")
    lines.append("rm -rf ~/ws/voip")
    lines.append("ow forget ~/ws/voip")
    return lines


def test_rm_removes_pushed_branches_keeps_unpushed_and_the_directory_goes(lab):
    root = _workspace(lab)
    (root / "scratch.txt").write_text("x\n")  # with .ow/config.toml: two files outside the repos
    community, enterprise = (repos_dir() / f"{name}.git" for name, _ in REPOS)
    plan = _plan(2)

    dry = lab.ow("rm", "voip", "-n")

    assert dry.exit_code == 0
    assert dry.stdout.splitlines() == plan
    assert root.is_dir()
    assert str(root) in _index_lines()

    result = lab.ow("rm", "voip", "-y")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == plan
    assert not root.exists()
    assert not _ok(community, "rev-parse", "--verify", "refs/heads/b-pushed")
    assert _ok(enterprise, "rev-parse", "--verify", "refs/heads/b-kept")
    for name, _branch in REPOS:
        listing = lab.git(repos_dir() / f"{name}.git", "worktree", "list", "--porcelain")
        assert f"worktree {root / name}" not in listing
    assert str(root) not in _index_lines()


def test_rm_is_refused_by_any_dirty_worktree_and_nothing_runs(lab):
    root = _workspace(lab)
    community, enterprise = (repos_dir() / f"{name}.git" for name, _ in REPOS)
    (root / "community" / "README.md").write_text("changed\n")  # a tracked file, modified
    (root / "enterprise" / "untracked.txt").write_text("x\n")

    result = lab.ow("rm", "voip", "-y")

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        "# community: modified README.md",
        "# enterprise: untracked untracked.txt",
    ]
    # Nothing ran: the directory, both worktrees, both branches and the index entry stay.
    assert (root / "community" / "README.md").read_text() == "changed\n"
    assert (root / "enterprise" / "untracked.txt").is_file()
    assert _ok(community, "rev-parse", "--verify", "refs/heads/b-pushed")
    assert _ok(enterprise, "rev-parse", "--verify", "refs/heads/b-kept")
    assert str(root) in _index_lines()


def test_a_declined_prompt_removes_nothing(lab, monkeypatch):
    root = _workspace(lab)
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("rm", "voip", input="n\n")

    assert result.exit_code == 2
    assert result.stdout.count("Proceed?") == 1
    assert root.is_dir()
    assert str(root) in _index_lines()
