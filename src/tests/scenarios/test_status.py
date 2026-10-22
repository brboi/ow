"""`ow status` scenarios: the table, from git reads only.

A detached HEAD with the `(default)` base marker, an upstream equal to the base, a
rebase in progress started with plain git, a repo with no base at all, and the
write-nothing purity. Wording is never asserted: every line is built from the
fixture's own SHAs and git reads.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import add_repo, git_ok, index_bytes, init_workspace, workspace_tree, worktree_refs


def _init(lab, *, branch: str | None = None) -> tuple[Path, Path]:
    """A one-repo workspace, built with `ow repo add` + `ow init`."""
    remote = add_repo(lab)
    return init_workspace(lab, "community", branch=branch), remote


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
    """An upstream read from branch config that equals the base counts as no upstream."""
    ws, _remote = _init(lab, branch="18.0-voip")
    worktree = ws / "community"
    assert lab.git(worktree, "symbolic-ref", "--short", "HEAD") == "18.0-voip"
    assert not git_ok(worktree, "rev-parse", "--abbrev-ref", "@{u}")
    lab.git(repos_dir() / "community.git", "config", "branch.18.0-voip.remote", "origin")
    lab.git(repos_dir() / "community.git", "config", "branch.18.0-voip.merge", "refs/heads/18.0")

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "community\t18.0-voip\torigin/18.0\t(none)\t(even)"
    ]


def test_status_reads_the_upstream_from_branch_config(lab):
    """The upstream is the branch's config pair, counted against its remote-tracking ref."""
    ws, _remote = _init(lab, branch="18.0-voip")
    worktree = ws / "community"
    bare = repos_dir() / "community.git"
    lab.git(bare, "update-ref", "refs/remotes/origin/18.0-voip",
            lab.sha(bare, "refs/remotes/origin/18.0"))
    lab.git(bare, "config", "branch.18.0-voip.remote", "origin")
    lab.git(bare, "config", "branch.18.0-voip.merge", "refs/heads/18.0-voip")
    (worktree / "odoo-bin").write_text("local\n")
    lab.git(worktree, "add", "odoo-bin")
    lab.git(worktree, "commit", "-m", "local edit")

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0].split("\t")[3] == "origin/18.0-voip (ahead 1, behind 0)"


def test_status_shows_an_upstream_whose_remote_ref_is_missing(lab):
    ws, _remote = _init(lab, branch="18.0-voip")
    bare = repos_dir() / "community.git"
    lab.git(bare, "config", "branch.18.0-voip.remote", "origin")
    lab.git(bare, "config", "branch.18.0-voip.merge", "refs/heads/gone")

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines()[0].split("\t")[3] == "origin/gone"


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
    assert not git_ok(worktree, "rebase", "origin/18.0")

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    head = lab.git(worktree, "rev-parse", "HEAD")
    # While the conflicted commit is being replayed, HEAD is the onto commit
    # (origin/18.0), so the base counts are even; the local commit lives in the
    # rebase state. Pin both the HEAD identity and the one conflicted file.
    assert head == lab.sha(repos_dir() / "community.git", "refs/remotes/origin/18.0")
    assert result.stdout.splitlines() == [
        f"community\tdetached at {head[:10]}\torigin/18.0 (default)\t(none)"
        f"\t(even)\tdirty 1\trebase in progress"
    ]


def test_status_tty_output_is_aligned(monkeypatch):
    """When stdout is a TTY, columns are aligned with header."""
    from io import StringIO
    from ow.commands.status import _print_aligned
    
    # Create test output
    output = StringIO()
    monkeypatch.setattr("sys.stdout", output)
    
    headers = ["name", "state", "base", "counts"]
    rows = [
        "repo1\tbranch\torigin/1.0\tahead 1, behind 0",
        "repo2\tdetached\tno base\t(?)",
    ]
    
    _print_aligned(headers, rows)
    
    result = output.getvalue()
    lines = result.splitlines()
    
    # Header should be present
    assert "name" in lines[0]
    assert "state" in lines[0]
    # Columns should be separated by at least 2 spaces
    assert "  " in lines[0]
    # Data rows should also be padded
    for line in lines[1:]:
        assert "  " in line or len(line) == 0


def test_status_writes_nothing(lab):
    """`status` leaves the tree, the lock, the index and the refs byte-identical."""
    ws, _remote = _init(lab, branch="18.0-voip")
    worktree = ws / "community"
    tree = workspace_tree(ws)
    index = index_bytes()
    refs = worktree_refs(lab, worktree)

    result = lab.ow("status", cwd=ws)

    assert result.exit_code == 0
    assert workspace_tree(ws) == tree
    assert index_bytes() == index
    assert worktree_refs(lab, worktree) == refs


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
        f"community\tdetached at {head[:10]}\tno base\t(none)\tno base"
    ]
