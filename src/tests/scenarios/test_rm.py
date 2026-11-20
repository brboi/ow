"""`ow rm <ws>` scenarios: pushed vs kept branches, the all-or-nothing pre-flight.

The workspace is built with plain git, `write_workspace` and the index, like
`test_cli.py`: `rm` reads the same state however it came to be.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import repos_dir
from tests.conftest import add_worktree, git_ok, index_lines, make_bare

BARE = "~/.local/share/ow/repos"
REPOS = (("community", "b-pushed"), ("enterprise", "b-kept"))
BASE = "18.0"
DIRTY = "commit, stash or discard, then re-run"
# `.ow/` holds two files: the workspace config and the sidecar lock its write leaves behind.
OUTSIDE = ".local/ (3 files), .ow/ (2 files), .venv/, notes.md"
KEPT = (
    "# enterprise: kept unpushed branch b-kept; "
    f"delete it with `git -C {BARE}/enterprise.git branch -D b-kept`"
)


def _workspace(lab) -> Path:
    """A two-repo workspace: community on a pushed branch, enterprise on an unpushed one."""
    root = lab.ws("voip")
    root.mkdir(parents=True, exist_ok=True)
    for name, branch in REPOS:
        bare = make_bare(lab, name, {BASE: {"README.md": f"{name}\n"}}, base=BASE)
        add_worktree(lab, bare, root / name, branch=branch, base=BASE)
        if branch == "b-kept":
            (root / name / "local.txt").write_text("x\n")
            lab.git(root / name, "add", "local.txt")
            lab.git(root / name, "commit", "-m", "local work")
    (root / ".local" / "share").mkdir(parents=True)
    for i in range(3):
        (root / ".local" / "share" / f"note{i}.txt").write_text("x\n")
    (root / ".venv").mkdir()
    (root / "notes.md").write_text("x\n")
    write_workspace(
        root / ".ow" / "config.toml",
        {repo: f"origin/{BASE}" for repo, _ in REPOS},
        {},
        declared={},
    )
    index_add(root)
    return root


def _plan() -> list[str]:
    """The whole plan `ow rm voip` prints: the outside entries, the notes, the steps."""
    return [
        f"# {OUTSIDE}",
        KEPT,
        f"git -C {BARE}/community.git worktree remove ~/ws/voip/community",
        f"git -C {BARE}/community.git branch -D b-pushed",
        f"git -C {BARE}/enterprise.git worktree remove ~/ws/voip/enterprise",
        "rm -rf ~/ws/voip",
        "# drop ~/ws/voip from the index",
    ]


def test_rm_removes_pushed_branches_keeps_unpushed_and_the_directory_goes(lab):
    root = _workspace(lab)
    community, enterprise = (repos_dir() / f"{name}.git" for name, _ in REPOS)
    plan = _plan()

    dry = lab.ow("rm", "voip", "-n")

    assert dry.exit_code == 0
    assert dry.stdout.splitlines() == plan
    assert root.is_dir()
    assert str(root) in index_lines()

    result = lab.ow("rm", "voip", "-y")

    assert result.exit_code == 0
    assert result.stdout.splitlines() == plan
    assert not root.exists()
    assert not git_ok(community, "rev-parse", "--verify", "refs/heads/b-pushed")
    assert git_ok(enterprise, "rev-parse", "--verify", "refs/heads/b-kept")
    for name, _branch in REPOS:
        listing = lab.git(repos_dir() / f"{name}.git", "worktree", "list", "--porcelain")
        assert f"worktree {root / name}" not in listing
    assert str(root) not in index_lines()


def test_rm_is_refused_by_any_dirty_worktree_and_nothing_runs(lab):
    root = _workspace(lab)
    community, enterprise = (repos_dir() / f"{name}.git" for name, _ in REPOS)
    (root / "community" / "README.md").write_text("changed\n")  # a tracked file, modified
    (root / "enterprise" / "untracked.txt").write_text("x\n")

    result = lab.ow("rm", "voip", "-y")

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        f"# community: modified README.md; {DIRTY}",
        f"# enterprise: untracked untracked.txt; {DIRTY}",
    ]
    # Nothing ran: the directory, both worktrees, both branches and the index entry stay.
    assert (root / "community" / "README.md").read_text() == "changed\n"
    assert (root / "enterprise" / "untracked.txt").is_file()
    assert git_ok(community, "rev-parse", "--verify", "refs/heads/b-pushed")
    assert git_ok(enterprise, "rev-parse", "--verify", "refs/heads/b-kept")
    assert str(root) in index_lines()


def test_rm_refuses_commits_that_live_on_no_branch(lab):
    root = _workspace(lab)
    community = repos_dir() / "community.git"
    lab.git(root / "community", "checkout", "--detach", "HEAD")
    (root / "community" / "detached.txt").write_text("x\n")
    lab.git(root / "community", "add", "detached.txt")
    lab.git(root / "community", "commit", "-m", "work on no branch")
    sha = lab.sha(root / "community", "HEAD")

    result = lab.ow("rm", "voip", "-y")

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        "# community: 1 commit(s) on no branch; keep them with "
        "`git -C ~/ws/voip/community switch -c <b>`, or discard them, then re-run"
    ]
    assert root.is_dir()
    assert git_ok(community, "rev-parse", "--verify", f"{sha}^{{commit}}")
    assert str(root) in index_lines()


def test_rm_refuses_a_second_worktree_of_the_same_bare(lab):
    root = _workspace(lab)
    community = repos_dir() / "community.git"
    lab.git(community, "worktree", "add", "--detach", str(root / "community-review"), f"origin/{BASE}")

    result = lab.ow("rm", "voip", "-y")

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        "# community-review: a checkout ow does not manage; move it away or delete it, then re-run"
    ]
    assert root.is_dir()
    assert (root / "community-review").is_dir()
    assert str(root) in index_lines()


def test_a_declined_prompt_removes_nothing(lab, monkeypatch):
    root = _workspace(lab)
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("rm", "voip", input="n\n")

    assert result.exit_code == 2
    assert result.stdout.count("Proceed?") == 1
    assert root.is_dir()
    assert str(root) in index_lines()
