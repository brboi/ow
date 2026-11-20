"""The pushed rule and the branch-removal step, against a real bare."""

from __future__ import annotations

from pathlib import Path

import pytest

from ow.errors import Refused
from ow.recipes.removal import branch_is_pushed, branch_removal
from tests.conftest import git, git_ok


def _bare(tmp_path: Path) -> Path:
    """A bare that has fetched `origin/master`, so a branch at it is pushed."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(tmp_path, "init", "-q", str(origin))
    (origin / "a.txt").write_text("a\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "one")
    git(origin, "branch", "-M", "master")

    bare = tmp_path / "bare.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(bare, "remote", "add", "origin", str(origin))
    git(bare, "fetch", "-q", "origin", "master")
    return bare


def _unpushed(bare: Path, branch: str) -> str:
    """Create `branch` at a commit no ref but itself reaches; return its SHA."""
    tree = git(bare, "rev-parse", "origin/master^{tree}")
    sha = git(bare, "commit-tree", tree, "-m", "local only")
    git(bare, "branch", branch, sha)
    return sha


def test_a_branch_at_a_remote_tip_is_pushed(tmp_path):
    bare = _bare(tmp_path)
    git(bare, "branch", "pushed", "origin/master")
    assert branch_is_pushed(bare, "pushed") is True


def test_a_branch_no_remote_reaches_is_not_pushed(tmp_path):
    bare = _bare(tmp_path)
    _unpushed(bare, "kept")
    assert branch_is_pushed(bare, "kept") is False


def test_a_branch_that_does_not_resolve_is_not_pushed(tmp_path):
    assert branch_is_pushed(_bare(tmp_path), "nope") is False


def test_branch_removal_keeps_an_unpushed_branch_and_names_the_delete_command(tmp_path):
    bare = _bare(tmp_path)
    sha = _unpushed(bare, "kept")

    steps, notes = branch_removal(bare, "kept")

    assert steps == ()
    assert len(notes) == 1
    assert notes[0].startswith("kept unpushed branch kept; delete it with `git -C ")
    assert notes[0].endswith("branch -D kept`")
    assert git(bare, "rev-parse", "refs/heads/kept") == sha


def test_branch_removal_deletes_a_pushed_branch(tmp_path):
    bare = _bare(tmp_path)
    git(bare, "branch", "pushed", "origin/master")

    steps, notes = branch_removal(bare, "pushed")

    assert notes == ()
    assert len(steps) == 1
    assert steps[0].shown.endswith("branch -D pushed")
    steps[0].run()
    assert not git_ok(bare, "rev-parse", "--verify", "--quiet", "refs/heads/pushed")


def test_branch_removal_refuses_when_the_tip_moved_after_planning(tmp_path):
    bare = _bare(tmp_path)
    git(bare, "branch", "pushed", "origin/master")
    steps, _ = branch_removal(bare, "pushed")

    # A commit lands on the branch while the prompt is open.
    moved = _unpushed(bare, "other")
    git(bare, "update-ref", "refs/heads/pushed", moved)

    with pytest.raises(Refused):
        steps[0].run()

    # The new tip is still there: nothing was deleted.
    assert git(bare, "rev-parse", "refs/heads/pushed") == moved


def test_branch_removal_raises_on_git_delete_failure_and_run_batch_reports_it(tmp_path):
    from ow.core.git import GitFailed
    from ow.core.plan import Batch, RepoPlan, run_batch

    bare = _bare(tmp_path)
    wt = tmp_path / "wt"
    git(bare, "worktree", "add", "-b", "checked-out", str(wt), "origin/master")

    steps, _ = branch_removal(bare, "checked-out")
    assert len(steps) == 1
    with pytest.raises(GitFailed):
        steps[0].run()

    # Running the failing OwStep through run_batch reports cleanly (no TypeError).
    outcome = run_batch(
        Batch(root=None, repos=(RepoPlan(repo="bare", steps=steps),)),
        prompt=False,
        dry_run=False,
        yes=True,
    )
    assert outcome.failed == ("bare",)
    assert outcome.exit_code == 1
