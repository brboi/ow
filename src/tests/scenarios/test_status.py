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
    assert lab.ow("status", "--fetch", cwd=ws).exit_code == 0
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


def test_status_multi_stack_tty_dedupes_services_hides_non_odoo_port_and_aligns_globally(lab, monkeypatch):
    """Multi-stack TTY status renders airy sections, hides phantom ports on non-Odoo stacks, and shows services once."""
    from io import StringIO

    from ow.commands import status as status_cmd
    from ow.core.plugins import StatusInfo, load_plugins

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"ent/__manifest__.py": "{}"}})
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    ws = lab.home / "porting"
    ws.mkdir(parents=True)
    assert lab.ow("init", "odoo=@18.0", "voipstack=voip", cwd=ws).exit_code == 0

    # Piped mode: voipstack has no [port ...] and services aren't repeated
    piped = lab.ow("status", cwd=ws)
    assert piped.exit_code == 0, piped.stderr
    assert "== odoo (" in piped.stdout and "[port 8069]" in piped.stdout
    assert "== voipstack (" in piped.stdout
    voip_header = next(line for line in piped.stdout.splitlines() if line.startswith("== voipstack "))
    assert "[port " not in voip_header

    # TTY mode with synthetic global service infos: printed ONCE in `services` block
    class _FakeTty(StringIO):
        def isatty(self) -> bool:
            return True

    fake_out = _FakeTty()
    monkeypatch.setattr("sys.stdout", fake_out)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        status_cmd,
        "_collect_plugin_status",
        lambda plugins, *, ws, facts, spec, offline: [
            StatusInfo(plugin="postgres", scope="service", subject="postgres", summary="running on localhost:5432"),
            StatusInfo(plugin="mailsink", scope="service", subject="mailsink", summary="running on smtp://localhost:1025"),
        ],
    )

    rc = status_cmd.run_multi(load_plugins(), multi_root=ws, offline=True)
    assert rc == 0
    out = fake_out.getvalue()
    assert out.startswith("porting  ")
    assert "odoo  :8069" in out
    assert "\nvoipstack\n" in out
    assert out.count("running on localhost:5432") == 1
    assert out.count("running on smtp://localhost:1025") == 1
    assert "\nservices\n" in out
    # Empty optional columns (`upstream`, `dirty`, `operation`) do not clutter rows
    assert "(none)" not in out
    assert "dirty" not in out
