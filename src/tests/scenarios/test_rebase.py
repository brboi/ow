"""`ow rebase` scenarios: the shared-branch guard, `--base`, `--autostash`, purity.

The workspace's branch tracks the branch the collaborator publishes, so a
colleague's push shows up as new upstream commits the guard must take in
without rewriting them. `ow rebase -n` fetches (remote-tracking refs are the
one thing a dry run may move), then prints the plan and writes nothing else;
an `n` answer leaves the workspace byte-identical and exits 2.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import (
    add_repo,
    git,
    git_ok,
    index_bytes,
    init_workspace,
    workspace_tree,
    worktree_refs,
)

BARE = "~/.local/share/ow/repos"
BRANCH = "18.0-voip-boi"
COMMUNITY = f"{BARE}/community.git"


def _workspace(lab, branches: dict[str, dict[str, str]] | None = None):
    """A workspace on `18.0-voip-boi`, tracking the branch the collaborator publishes."""
    remote = add_repo(lab, "community", branches)
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"shared.py": "shared\n"}, message="shared", branch=BRANCH)
    collaborator.push(BRANCH)
    ws = init_workspace(lab, "community", branch=BRANCH)
    return ws, ws / "community", collaborator


def _local_commit(lab, worktree: Path) -> str:
    """One unpushed commit on the worktree's branch; returns its sha."""
    (worktree / "local.py").write_text("local\n")
    lab.git(worktree, "add", "local.py")
    lab.git(worktree, "commit", "-m", "local work")
    return lab.git(worktree, "rev-parse", "HEAD")


def _fetches(result) -> list[str]:
    """The bare fetch lines; their shape belongs to the fetch recipe's own tests."""
    return [
        line for line in result.stdout.splitlines() if line.startswith(f"git -C {COMMUNITY} fetch")
    ]


def _rebase_lines(result) -> list[str]:
    """The plan's lines minus the fetches, which the fetch recipe renders."""
    fetch = _fetches(result)
    return [line for line in result.stdout.splitlines() if line not in fetch]


def _git_path(worktree: Path, name: str) -> Path:
    """A worktree's git path, resolved (git prints it relative to the -C directory)."""
    raw = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-path", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = Path(raw)
    return path if path.is_absolute() else worktree / path


def test_colleague_pushed_lands_the_new_commits_with_a_cherry_pick(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    assert _rebase_lines(result) == [
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --ff --empty=drop {taken}..origin/{BRANCH}",
    ]

    # The colleague's commit landed above the local one; nothing is left over.
    assert lab.git(worktree, "log", "--format=%s", "-2").splitlines() == [
        "colleague",
        "local work",
    ]
    assert (worktree / "colleague.py").read_text() == "colleague\n"
    assert not _git_path(worktree, "CHERRY_PICK_HEAD").exists()

    # A re-run is a no-op: the patch is already in, so the plan is empty.
    head = lab.git(worktree, "rev-parse", "HEAD")
    again = lab.ow("rebase", "-y", cwd=ws)

    assert again.exit_code == 0
    assert _rebase_lines(again) == []
    assert lab.git(worktree, "rev-parse", "HEAD") == head


def test_a_colleague_push_onto_a_branch_with_no_local_commits_lands_on_the_upstream(
    lab, monkeypatch
):
    """The guard fast-forwards, so the branch is `@{u}` itself and nothing prompts."""
    ws, worktree, collaborator = _workspace(lab)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)
    upstream = lab.git(collaborator.path, "rev-parse", BRANCH)
    # A copy would take this committer date and so a different SHA: only a real
    # fast-forward can leave the branch on the upstream's own commit.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2024-06-01T00:00:00Z")

    result = lab.ow("rebase", cwd=ws)

    assert result.exit_code == 0
    assert _rebase_lines(result) == [
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --ff --empty=drop {taken}..origin/{BRANCH}",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream
    assert git_ok(worktree, "merge-base", "--is-ancestor", f"origin/{BRANCH}", "HEAD")


def test_a_second_run_skips_the_copy_the_first_one_made(lab):
    """`last_taken` moves past a copy, so the taken commit is not picked again."""
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)
    assert lab.ow("rebase", "-y", cwd=ws).exit_code == 0
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")

    collaborator.commit({"later.py": "later\n"}, message="later")
    collaborator.push(BRANCH)

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    # The range starts at the commit the copy stands for, not at the stale tip.
    assert _rebase_lines(result) == [
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --ff --empty=drop {taken}..origin/{BRANCH}",
    ]
    assert lab.git(worktree, "log", "--format=%s", "-2").splitlines() == [
        "later",
        "colleague",
    ]


def test_mixed_force_push_and_exhausted_reflog_falls_back_to_fork_point(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    # The colleague rewrites the shared branch from the base, so no upstream
    # value this branch ever took in survives the force-push.
    collaborator.git("reset", "--hard", lab.sha(collaborator.path, "origin/18.0"))
    collaborator.commit({"rewritten.py": "rewritten\n"}, message="rewritten")
    collaborator.git("push", "--force", "origin", BRANCH)

    # Take the rewrite in, then let the reflog's unreachable entries expire:
    # nothing the branch ever took in is left to fast-forward from.
    bare = repos_dir() / "community.git"
    lab.git(bare, "fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    lab.git(bare, "reflog", "expire", "--expire-unreachable=now",
            f"refs/remotes/origin/{BRANCH}")

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    assert _rebase_lines(result) == [
        f"git -C community rebase --fork-point origin/{BRANCH}",
    ]
    assert git_ok(worktree, "merge-base", "--is-ancestor", f"origin/{BRANCH}", "HEAD")
    assert (worktree / "rewritten.py").read_text() == "rewritten\n"
    assert not _git_path(worktree, "rebase-merge").exists()


def test_a_force_push_takes_the_rewrite_in_through_fork_point(lab):
    """A rewritten upstream: the taken value is no ancestor, so `--fork-point` runs."""
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    collaborator.git("reset", "--hard", lab.sha(collaborator.path, "origin/18.0"))
    collaborator.commit({"rewritten.py": "rewritten\n"}, message="rewritten")
    collaborator.git("push", "--force", "origin", BRANCH)

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    # The fetch moved the ref backwards, so git reported it and ow notes it.
    assert _rebase_lines(result) == [
        f"# community: origin/{BRANCH} was force-pushed",
        f"git -C community rebase --fork-point origin/{BRANCH}",
    ]
    assert git_ok(worktree, "merge-base", "--is-ancestor", f"origin/{BRANCH}", "HEAD")
    assert (worktree / "rewritten.py").read_text() == "rewritten\n"
    assert not _git_path(worktree, "rebase-merge").exists()


def test_a_merge_in_the_upstream_range_falls_back_to_fork_point(lab):
    """A cherry-pick cannot replay a merge, so the fork-point path runs instead.

    The moved base is in the colleague's branch, so the fork-point step lands it
    too: a base step of its own would replay the colleague's commits.
    """
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    collaborator.git("checkout", "18.0")
    collaborator.commit({"base.py": "base\n"}, message="base move")
    collaborator.git("push", "origin", "18.0")
    collaborator.git("checkout", BRANCH)
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.git("fetch", "origin")
    collaborator.git("merge", "--no-edit", "origin/18.0")
    collaborator.push(BRANCH)

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    assert _rebase_lines(result) == [
        f"git -C community rebase --fork-point origin/{BRANCH}",
    ]
    assert git_ok(worktree, "merge-base", "--is-ancestor", f"origin/{BRANCH}", "HEAD")
    assert lab.git(worktree, "log", "--format=%s", "-1") == "local work"
    assert not _git_path(worktree, "rebase-merge").exists()


def test_a_conflict_prints_the_continuation_then_the_remaining_commands(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    # The colleague moves the base with a change the branch's own commit clashes with.
    collaborator.git("checkout", "18.0")
    collaborator.commit({"shared.py": "base\n"}, message="base shared")
    collaborator.push("18.0")

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 1
    assert _rebase_lines(result)[0] == "git -C community rebase origin/18.0"
    assert "git -C community rebase --continue" in _rebase_lines(result)
    assert "git -C community rebase --abort" in _rebase_lines(result)
    assert _git_path(worktree, "rebase-merge").exists()

    # Resolving by hand and running the printed command completes the rebase.
    (worktree / "shared.py").write_text("resolved\n")
    git(worktree, "add", "shared.py")
    subprocess.run(
        ["git", "-C", str(worktree), "rebase", "--continue"],
        check=True, capture_output=True,
        env={**os.environ, "GIT_EDITOR": "true"},
    )
    assert lab.git(worktree, "log", "--format=%s", "-2").splitlines() == [
        "local work",
        "shared",
    ]
    assert git_ok(worktree, "merge-base", "--is-ancestor", "origin/18.0", "HEAD")
    assert not _git_path(worktree, "rebase-merge").exists()


def test_a_new_base_is_fetched_and_recorded_only_after_a_successful_rebase(lab):
    # `lab.remote` stacks its branches, so the clean one must come before the
    # conflicting one: 18.0-clean adds only a README line, 18.0-next also a shared.py.
    ws, worktree, collaborator = _workspace(
        lab,
        branches={
            "18.0": lab.odoo_files(),
            "18.0-clean": {"README.md": "clean\n"},
            "18.0-next": {"shared.py": "next\n"},
        },
    )
    _local_commit(lab, worktree)
    bare = repos_dir() / "community.git"

    conflict = lab.ow("rebase", "--base", "18.0-next", "-y", cwd=ws)

    assert conflict.exit_code == 1
    assert _rebase_lines(conflict)[:2] == [
        "git -C community rebase --onto origin/18.0-next origin/18.0",
        f"git -C community config branch.{BRANCH}.ow-base origin/18.0-next",
    ]
    # The rebase stopped, so the recorded base kept its old value.
    assert lab.git(bare, "config", "--get", f"branch.{BRANCH}.ow-base") == "origin/18.0"

    lab.git(worktree, "rebase", "--abort")

    clean = lab.ow("rebase", "--base", "18.0-clean", "-y", cwd=ws)

    assert clean.exit_code == 0
    assert _rebase_lines(clean) == [
        "git -C community rebase --onto origin/18.0-clean origin/18.0",
        f"git -C community config branch.{BRANCH}.ow-base origin/18.0-clean",
    ]
    assert lab.git(bare, "config", "--get", f"branch.{BRANCH}.ow-base") == "origin/18.0-clean"
    assert git_ok(worktree, "merge-base", "--is-ancestor", "origin/18.0-clean", "HEAD")


def test_a_base_shorthand_is_parsed_and_fetched(lab):
    """`--base 19.0` becomes `origin/19.0`, which ow fetches itself."""
    ws, worktree, collaborator = _workspace(
        lab,
        branches={
            "18.0": lab.odoo_files(),
            "19.0": {"odoo/release.py": "NINETEEN = 19\n"},
        },
    )
    _local_commit(lab, worktree)
    bare = repos_dir() / "community.git"

    result = lab.ow("rebase", "--base", "19.0", "-y", cwd=ws)

    assert result.exit_code == 0
    assert any("refs/heads/19.0" in line for line in _fetches(result))
    assert _rebase_lines(result) == [
        "git -C community rebase --onto origin/19.0 origin/18.0",
        f"git -C community config branch.{BRANCH}.ow-base origin/19.0",
    ]
    assert lab.git(bare, "config", "--get", f"branch.{BRANCH}.ow-base") == "origin/19.0"
    assert git_ok(worktree, "merge-base", "--is-ancestor", "origin/19.0", "HEAD")


def test_a_base_re_run_that_head_already_contains_only_writes_the_config(lab):
    """A skipped config write must not let a re-run replay the new base onto itself."""
    ws, worktree, collaborator = _workspace(
        lab,
        branches={
            "18.0": lab.odoo_files(),
            "18.0-clean": {"README.md": "clean\n"},
        },
    )
    _local_commit(lab, worktree)
    bare = repos_dir() / "community.git"
    assert lab.ow("rebase", "--base", "18.0-clean", "-y", cwd=ws).exit_code == 0
    head = lab.git(worktree, "rev-parse", "HEAD")
    lab.git(bare, "config", f"branch.{BRANCH}.ow-base", "origin/18.0")

    again = lab.ow("rebase", "--base", "18.0-clean", "-y", cwd=ws)

    assert again.exit_code == 0
    assert _rebase_lines(again) == [
        f"git -C community config branch.{BRANCH}.ow-base origin/18.0-clean",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert lab.git(bare, "config", "--get", f"branch.{BRANCH}.ow-base") == "origin/18.0-clean"


def test_autostash_keeps_the_stash_and_prints_the_pop_last(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    collaborator.git("checkout", "18.0")
    collaborator.commit({"shared.py": "base\n"}, message="base shared")
    collaborator.push("18.0")
    (worktree / "odoo-bin").write_text("dirty\n")

    result = lab.ow("rebase", "--autostash", "-y", cwd=ws)

    assert result.exit_code == 1
    lines = _rebase_lines(result)
    assert lines[:3] == [
        "git -C community stash push -m 'ow rebase --autostash'",
        "git -C community rebase origin/18.0",
        "git -C community stash pop",
    ]
    # The rebase stopped before the pop, so the stash is kept for the resume.
    assert lines[-1] == "git -C community stash pop"
    assert "ow rebase --autostash" in lab.git(worktree, "stash", "list")
    assert _git_path(worktree, "rebase-merge").exists()


def test_dry_run_prints_the_plan_and_moves_nothing(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)

    tree = workspace_tree(ws)
    index = index_bytes()
    head = lab.git(worktree, "rev-parse", "HEAD")
    refs = worktree_refs(lab, worktree, remote_tracking=False)

    result = lab.ow("rebase", "-n", cwd=ws)

    assert result.exit_code == 0
    assert _rebase_lines(result) == [
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --ff --empty=drop {taken}..origin/{BRANCH}",
    ]

    # Purity: the fetch moved the remote-tracking ref; nothing else changed.
    assert workspace_tree(ws) == tree
    assert index_bytes() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert worktree_refs(lab, worktree, remote_tracking=False) == refs


def test_a_dirty_repo_with_an_empty_plan_succeeds(lab):
    """With nothing to rebase, a modified file is not a reason to refuse."""
    ws, worktree, collaborator = _workspace(lab)
    (worktree / "odoo-bin").write_text("dirty\n")

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    assert _rebase_lines(result) == []
    assert (worktree / "odoo-bin").read_text() == "dirty\n"


def test_a_declined_prompt_changes_nothing(lab, monkeypatch):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)

    tree = workspace_tree(ws)
    index = index_bytes()
    head = lab.git(worktree, "rev-parse", "HEAD")
    refs = worktree_refs(lab, worktree, remote_tracking=False)

    # The prompt is offered only when `ow.term.interactive()` is true;
    # `run_batch` calls it through the module, so one patch site is enough.
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("rebase", input="n\n", cwd=ws)

    assert result.exit_code == 2
    assert _rebase_lines(result)[:2] == [
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --ff --empty=drop {taken}..origin/{BRANCH}",
    ]

    assert workspace_tree(ws) == tree
    assert index_bytes() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert worktree_refs(lab, worktree, remote_tracking=False) == refs
