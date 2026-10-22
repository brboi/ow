"""`ow reset` scenarios: the dry run's purity, the mixed reset, `--hard`, and a declined prompt.

The workspace's branch tracks the branch the collaborator publishes, so the reset moves
the branch back to `origin/18.0-voip`. `ow reset -n` fetches (remote-tracking refs are
the one thing a dry run may move), then prints the plan, and writes nothing else. `-y`
runs it without a question; an `n` answer leaves the workspace byte-identical and exits 2.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import (
    add_repo,
    index_bytes,
    init_workspace,
    workspace_tree,
    worktree_refs,
)

BARE = "~/.local/share/ow/repos"
# The base and the upstream are fetched in separate steps, each with its own refspec.
FETCH = (
    f"git -C {BARE}/community.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0",
    f"git -C {BARE}/community.git fetch origin"
    " +refs/heads/18.0-voip:refs/remotes/origin/18.0-voip",
)


def _workspace(lab, extra: dict[str, str] | None = None) -> tuple[Path, Path, object]:
    """A workspace on `18.0-voip`, tracking the branch the collaborator publishes."""
    remote = add_repo(lab, "community", {"18.0": {**lab.odoo_files(), **(extra or {})}})
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"feature.py": "v1\n"}, message="feature v1", branch="18.0-voip")
    collaborator.push("18.0-voip")
    ws = init_workspace(lab, "community", branch="18.0-voip")
    return ws, ws / "community", collaborator


def _local_commit(lab, worktree: Path) -> str:
    """One unpushed commit on the worktree's branch; returns its sha."""
    (worktree / "odoo-bin").write_text("local\n")
    lab.git(worktree, "add", "odoo-bin")
    lab.git(worktree, "commit", "-m", "local work")
    return lab.git(worktree, "rev-parse", "HEAD")


def test_reset_dry_run_fetches_plans_and_leaves_the_rest(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    tip = collaborator.commit({"feature.py": "v2\n"}, message="feature v2")
    collaborator.push("18.0-voip")

    tree = workspace_tree(ws)
    index = index_bytes()
    head = lab.git(worktree, "rev-parse", "HEAD")
    refs = worktree_refs(lab, worktree, remote_tracking=False)

    result = lab.ow("reset", "-n", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: drops 1 commit(s) on no remote",
        "git -C community reset origin/18.0-voip",
    ]

    # Purity: the fetch moved the remote-tracking ref; nothing else changed.
    assert workspace_tree(ws) == tree
    assert index_bytes() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert worktree_refs(lab, worktree, remote_tracking=False) == refs
    assert lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip") == tip


def test_reset_mixed_moves_head_only(lab):
    ws, worktree, _collaborator = _workspace(lab)
    local = _local_commit(lab, worktree)
    upstream = lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip")
    assert local != upstream

    result = lab.ow("reset", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: drops 1 commit(s) on no remote",
        "git -C community reset origin/18.0-voip",
    ]

    # HEAD moved back; the tree kept the dropped commit's content as a change.
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream
    assert (worktree / "odoo-bin").read_text() == "local\n"
    assert lab.git(worktree, "status", "--porcelain") == "M odoo-bin"


def test_reset_hard_restores_a_dirty_tree(lab):
    """HEAD is already the ref, but `--hard` still restores the dirty files."""
    ws, worktree, _collaborator = _workspace(lab, extra={"with space.txt": "clean\n"})
    upstream = lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip")
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream
    odoo_bin = (worktree / "odoo-bin").read_bytes()
    (worktree / "odoo-bin").write_text("dirty\n")
    (worktree / "with space.txt").write_text("dirty\n")

    result = lab.ow("reset", "--hard", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: modified: odoo-bin",
        "# community: modified: with space.txt",
        "git -C community reset --hard origin/18.0-voip",
    ]
    assert (worktree / "odoo-bin").read_bytes() == odoo_bin
    assert (worktree / "with space.txt").read_text() == "clean\n"
    assert lab.git(worktree, "status", "--porcelain") == ""


def test_reset_at_the_followed_ref_is_a_no_op(lab):
    ws, worktree, _collaborator = _workspace(lab)
    upstream = lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip")
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream

    result = lab.ow("reset", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [*FETCH]
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream


def test_reset_untracked_only_at_the_followed_ref_is_a_no_op(lab):
    """An untracked-only tree at the followed ref has nothing to do, even under --hard."""
    ws, worktree, _collaborator = _workspace(lab)
    upstream = lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip")
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream
    (worktree / "scratch.py").write_text("wip\n")

    result = lab.ow("reset", "--hard", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [*FETCH]
    assert (worktree / "scratch.py").read_text() == "wip\n"


def test_reset_declined_prompt_changes_nothing(lab, monkeypatch):
    ws, worktree, _collaborator = _workspace(lab)
    head = _local_commit(lab, worktree)

    tree = workspace_tree(ws)
    index = index_bytes()
    refs = worktree_refs(lab, worktree, remote_tracking=False)

    # `run_batch` reads `ow.term.interactive` through the module, so one patch site is enough.
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("reset", input="n\n", cwd=ws)

    assert result.exit_code == 2
    lines = result.stdout.splitlines()
    assert lines[2] == "# community: drops 1 commit(s) on no remote"
    assert lines[3] == "git -C community reset origin/18.0-voip"

    assert workspace_tree(ws) == tree
    assert index_bytes() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert worktree_refs(lab, worktree, remote_tracking=False) == refs


def test_reset_skips_on_failed_fetch(lab):
    """A failed fetch skips the repo with the standard refusal and exits 1."""
    ws, worktree, _collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    head = lab.git(worktree, "rev-parse", "HEAD")
    lab.git(repos_dir() / "community.git", "remote", "set-url", "origin", "/nonexistent/ow")

    result = lab.ow("reset", "-y", cwd=ws)

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: fetch failed; pass --no-fetch to work on cached refs",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == head


def test_reset_hard_lists_untracked_collisions(lab):
    """An untracked file --hard would overwrite is listed before the prompt."""
    ws, worktree, collaborator = _workspace(lab)
    collaborator.commit({"notes.txt": "upstream\n"}, message="add notes")
    collaborator.push("18.0-voip")
    (worktree / "notes.txt").write_text("MY-PRECIOUS\n")
    assert lab.git(worktree, "status", "--porcelain") == "?? notes.txt"

    result = lab.ow("reset", "--hard", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: untracked, overwritten: notes.txt",
        "git -C community reset --hard origin/18.0-voip",
    ]
    assert (worktree / "notes.txt").read_text() == "upstream\n"


def test_reset_behind_only_uses_keep(lab):
    """A behind-only reset (nothing dropped) uses --keep and keeps local edits."""
    ws, worktree, collaborator = _workspace(lab)
    (worktree / "odoo-bin").write_text("local edit\n")
    tip = collaborator.commit({"feature.py": "v2\n"}, message="feature v2")
    collaborator.push("18.0-voip")

    result = lab.ow("reset", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        *FETCH,
        "git -C community reset --keep origin/18.0-voip",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == tip
    assert (worktree / "odoo-bin").read_text() == "local edit\n"


def test_reset_no_terminal_no_yes_exits_2(lab, monkeypatch):
    """Reset without TTY and without -y exits 2."""
    ws, worktree, _collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    # Simulate no terminal
    monkeypatch.setattr("ow.term.interactive", lambda: False)

    result = lab.ow("reset", cwd=ws)

    assert result.exit_code == 2