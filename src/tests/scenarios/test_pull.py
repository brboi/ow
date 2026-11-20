"""`ow pull` scenarios (§7/§16): the fast-forward, the divergence, and `--no-fetch`.

The workspace's branch tracks the branch the collaborator publishes, so `ow pull` is a
`merge --ff-only` onto a fresh `@{u}`. A HEAD with commits of its own is left alone with
a note, and `--no-fetch` plans against the cached refs.
"""

from __future__ import annotations

from pathlib import Path

BARE = "~/.local/share/ow/repos"


def _workspace(lab) -> tuple[Path, object]:
    """A workspace on `18.0-voip`, tracking the branch the collaborator publishes."""
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"feature.py": "v1\n"}, message="feature v1", branch="18.0-voip")
    collaborator.push("18.0-voip")
    ws = lab.ws("voip")
    assert (
        lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "18.0-voip").exit_code
        == 0
    )
    return ws, collaborator


def test_pull_fast_forwards_to_the_followed_ref(lab):
    ws, collaborator = _workspace(lab)
    worktree = ws / "community"
    tip = collaborator.commit({"feature.py": "v2\n"}, message="feature v2")
    collaborator.push("18.0-voip")
    before = lab.git(worktree, "rev-parse", "HEAD")
    assert before != tip

    result = lab.ow("pull", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip",
        "git -C community merge --ff-only origin/18.0-voip",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == tip


def test_pull_leaves_a_diverged_head_alone(lab):
    ws, collaborator = _workspace(lab)
    worktree = ws / "community"
    (worktree / "local.txt").write_text("local\n")
    lab.git(worktree, "add", "local.txt")
    lab.git(worktree, "commit", "-m", "local work")
    collaborator.commit({"feature.py": "v2\n"}, message="feature v2")
    collaborator.push("18.0-voip")
    before = lab.git(worktree, "rev-parse", "HEAD")

    result = lab.ow("pull", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 18.0-voip",
        "# community: diverged from origin/18.0-voip — run `ow rebase`",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == before


def test_pull_no_fetch_uses_the_cached_refs(lab):
    ws, collaborator = _workspace(lab)
    worktree = ws / "community"
    tip = collaborator.commit({"feature.py": "v2\n"}, message="feature v2")
    collaborator.push("18.0-voip")
    assert lab.ow("fetch", cwd=ws).exit_code == 0

    result = lab.ow("pull", "--no-fetch", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["git -C community merge --ff-only origin/18.0-voip"]
    assert lab.git(worktree, "rev-parse", "HEAD") == tip