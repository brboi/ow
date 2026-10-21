"""`ow reset` scenarios (§7/§16): the dry run's purity, the mixed reset, `--hard`, and a
declined prompt.

The workspace's branch tracks the branch the collaborator publishes, so the reset moves
the branch back to `origin/18.0-voip`. `ow reset -n` fetches (remote-tracking refs are
the one thing a dry run may move), then prints the plan, and writes nothing else. `-y`
runs it without a question; an `n` answer leaves the workspace byte-identical and exits 2.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.paths import index_file, repos_dir

BARE = "~/.local/share/ow/repos"


def _workspace(lab, extra: dict[str, str] | None = None) -> tuple[Path, Path, object]:
    """A workspace on `18.0-voip`, tracking the branch the collaborator publishes."""
    remote = lab.remote(
        "community", {"18.0": {**lab.odoo_files(), **(extra or {})}}, head="18.0"
    )
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"feature.py": "v1\n"}, message="feature v1", branch="18.0-voip")
    collaborator.push("18.0-voip")
    ws = lab.ws("voip")
    assert (
        lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "18.0-voip").exit_code
        == 0
    )
    return ws, ws / "community", collaborator


def _local_commit(lab, worktree: Path) -> str:
    """One unpushed commit on the worktree's branch; returns its sha."""
    (worktree / "odoo-bin").write_text("local\n")
    lab.git(worktree, "add", "odoo-bin")
    lab.git(worktree, "commit", "-m", "local work")
    return lab.git(worktree, "rev-parse", "HEAD")


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by relative path — the workspace tree and the lock."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _index() -> bytes:
    """The workspace index file's bytes."""
    path = index_file()
    return path.read_bytes() if path.exists() else b""


def _refs(lab, worktree: Path) -> dict[str, str]:
    """The worktree's refs, minus the remote-tracking ones a fetch may move."""
    output = lab.git(worktree, "for-each-ref", "--format=%(refname) %(objectname)")
    return {
        name: sha
        for name, sha in (line.split(" ", 1) for line in output.splitlines())
        if not name.startswith("refs/remotes/")
    }


def test_reset_dry_run_fetches_plans_and_leaves_the_rest(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    tip = collaborator.commit({"feature.py": "v2\n"}, message="feature v2")
    collaborator.push("18.0-voip")

    tree = _tree(ws)
    index = _index()
    head = lab.git(worktree, "rev-parse", "HEAD")
    refs = _refs(lab, worktree)

    result = lab.ow("reset", "-n", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip",
        "# community: drops 1 commit(s) on no remote",
        "git -C community reset origin/18.0-voip",
    ]

    # Purity: the fetch moved the remote-tracking ref; nothing else changed.
    assert _tree(ws) == tree
    assert _index() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert _refs(lab, worktree) == refs
    assert lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip") == tip


def test_reset_mixed_moves_head_only(lab):
    ws, worktree, _collaborator = _workspace(lab)
    local = _local_commit(lab, worktree)
    upstream = lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0-voip")
    assert local != upstream

    result = lab.ow("reset", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip",
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
        f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip",
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
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip"
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == upstream


def test_reset_declined_prompt_changes_nothing(lab, monkeypatch):
    ws, worktree, _collaborator = _workspace(lab)
    head = _local_commit(lab, worktree)

    tree = _tree(ws)
    index = _index()
    refs = _refs(lab, worktree)

    # P9: the prompt is offered only when `ow.term.interactive()` is true;
    # `run_batch` calls it through the module, so one patch site is enough.
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("reset", input="n\n", cwd=ws)

    assert result.exit_code == 2
    lines = result.stdout.splitlines()
    assert lines[1] == "# community: drops 1 commit(s) on no remote"
    assert lines[2] == "git -C community reset origin/18.0-voip"

    assert _tree(ws) == tree
    assert _index() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert _refs(lab, worktree) == refs