"""`ow pull` scenarios: the fast-forward, the divergence refusal, and `--no-fetch`.

The workspace's branch tracks the branch the collaborator publishes, so `ow pull` is a
`merge --ff-only` onto a fresh `@{u}`. A HEAD with commits of its own (diverged) is refused
with exit 1, and `--no-fetch` plans against the cached refs.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import add_repo, init_workspace

BARE = "~/.local/share/ow/repos"
# The base and the upstream are fetched in separate steps, each with its own refspec.
FETCH = (
    f"git -C {BARE}/community.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0",
    f"git -C {BARE}/community.git fetch origin"
    " +refs/heads/18.0-voip:refs/remotes/origin/18.0-voip",
)


def _workspace(lab) -> tuple[Path, object]:
    """A workspace on `18.0-voip`, tracking the branch the collaborator publishes."""
    remote = add_repo(lab)
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"feature.py": "v1\n"}, message="feature v1", branch="18.0-voip")
    collaborator.push("18.0-voip")
    ws = init_workspace(lab, "community", branch="18.0-voip")
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
        *FETCH,
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

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: diverged from origin/18.0-voip; run `ow rebase`",
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


def test_pull_skips_on_failed_fetch(lab):
    """A failed fetch skips the repo with the standard refusal and exits 1."""
    ws, _collaborator = _workspace(lab)
    worktree = ws / "community"
    head = lab.git(worktree, "rev-parse", "HEAD")
    lab.git(repos_dir() / "community.git", "remote", "set-url", "origin", "/nonexistent/ow")

    result = lab.ow("pull", cwd=ws)

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        *FETCH,
        "# community: fetch failed; pass --no-fetch to work on cached refs",
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == head