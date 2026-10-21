"""`ow status` scenarios (§7/§16): the table, from git reads only.

A detached HEAD with the `(default)` base marker, an upstream equal to the base, a
rebase in progress started with plain git, a repo with no base at all, and the
write-nothing purity of §16. Wording is never asserted: every line is built from the
fixture's own SHAs and git reads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ow.core.paths import index_file, repos_dir


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command succeeds; the rebase conflict is a state under test."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


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
    """The worktree's refs, name -> sha; `status` must move none of them."""
    output = lab.git(worktree, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in output.splitlines())


def _init(lab, *, branch: str | None = None) -> tuple[Path, Path]:
    """A one-repo workspace, built with `ow repo add` + `ow init` (§6)."""
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    args = ["init", str(lab.ws("voip")), "community", "--base", "18.0"]
    if branch is not None:
        args += ["--branch", branch]
    assert lab.ow(*args).exit_code == 0
    return lab.ws("voip"), remote


def test_status_on_a_detached_head_and_a_defaulted_base(lab):
    """Without `--branch` the worktree is detached at the default base, so it is marked."""
    ws, _remote = _init(lab)
    head = lab.git(ws / "community", "rev-parse", "HEAD")

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"community\tdetached at {head[:10]}\torigin/18.0 (default)\t(none)\t(even)"
    ]


def test_status_shows_an_upstream_equal_to_the_base_as_none(lab):
    """§5: `@{u}` equal to the base counts as no upstream."""
    ws, _remote = _init(lab, branch="18.0-voip")
    worktree = ws / "community"
    assert lab.git(worktree, "symbolic-ref", "--short", "HEAD") == "18.0-voip"
    assert not _ok(worktree, "rev-parse", "--abbrev-ref", "@{u}")
    lab.git(worktree, "branch", "--set-upstream-to", "origin/18.0")
    assert lab.git(worktree, "rev-parse", "@{u}") == lab.sha(
        repos_dir() / "community.git", "refs/remotes/origin/18.0"
    )

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "community\t18.0-voip\torigin/18.0\t(none)\t(even)"
    ]


def test_status_during_a_rebase(lab):
    """A rebase started with plain git is reported as the last field."""
    ws, remote = _init(lab, branch="18.0-voip")
    worktree = ws / "community"
    (worktree / "odoo-bin").write_text("local\n")
    lab.git(worktree, "add", "odoo-bin")
    lab.git(worktree, "commit", "-m", "local edit")
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"odoo-bin": "colleague\n"}, message="colleague edit")
    collaborator.push("18.0")
    assert lab.ow("fetch", cwd=ws).exit_code == 0
    assert not _ok(worktree, "rebase", "origin/18.0")

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    head = lab.git(worktree, "rev-parse", "HEAD")
    behind, ahead = (
        int(part)
        for part in lab.git(
            worktree, "rev-list", "--left-right", "--count", "origin/18.0...HEAD"
        ).split()
    )
    dirty = len(
        [
            line
            for line in lab.git(worktree, "status", "--porcelain=v2").splitlines()
            if line[:1] in "12u?"
        ]
    )
    counts = "(even)" if ahead == 0 and behind == 0 else f"ahead {ahead}, behind {behind}"
    assert result.stdout.splitlines() == [
        f"community\tdetached at {head[:10]}\torigin/18.0 (default)\t(none)"
        f"\t{counts}\tdirty {dirty}\trebase in progress"
    ]


def test_status_writes_nothing(lab):
    """§16: `status` leaves the tree, the lock, the index and the refs byte-identical."""
    ws, _remote = _init(lab, branch="18.0-voip")
    worktree = ws / "community"
    tree = _tree(ws)
    index = _index()
    refs = _refs(lab, worktree)

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert _tree(ws) == tree
    assert _index() == index
    assert _refs(lab, worktree) == refs


def test_status_without_a_base_reports_unset(lab):
    """A worktree with no `[repos]` entry and no `ow-base` has neither base nor counts."""
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    bare = repos_dir() / "community.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", str(remote))
    lab.git(bare, "fetch", "origin")
    root = lab.ws("voip")
    root.mkdir(parents=True)
    lab.git(bare, "worktree", "add", "--detach", str(root / "community"), "origin/18.0")
    (root / ".ow").mkdir()
    (root / ".ow" / "config.toml").write_text("")

    head = lab.git(root / "community", "rev-parse", "HEAD")

    result = lab.ow("status", cwd=root)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"community\tdetached at {head[:10]}\t(unset)\t(none)\t(unset)"
    ]