"""Workspace resolution: resolve_root, stray checkouts and relative worktree paths."""

from __future__ import annotations

import os
import shutil

from ow.core.paths import repos_dir
from ow.core.plugins import load_plugins
from ow.core.session import open_session
from ow.core.workspace import derive_repos
from tests.conftest import add_repo, init_workspace


def _session(root):
    return open_session(str(root), need_workspace=False, plugins=load_plugins())


def _init_workspace(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    return init_workspace(lab, "community", root=lab.home / "ws")


def test_dead_index_entry_is_refused_with_prune_hint(lab):
    """`-w <name>` on a deleted workspace errors and does not recreate it."""
    ws = _init_workspace(lab)
    shutil.rmtree(ws)

    result = lab.ow("refresh", "-w", "ws")

    assert result.exit_code == 2
    assert "missing" in result.stderr
    assert "run `ow prune`" in result.stderr
    assert not ws.exists()


def test_unknown_name_lists_the_known_names(lab):
    ws = _init_workspace(lab)
    assert ws.name == "ws"

    result = lab.ow("refresh", "-w", "nope")

    assert result.exit_code == 2
    assert "unknown workspace 'nope'" in result.stderr
    assert "ws" in result.stderr


def test_a_plain_clone_is_reported_as_a_stray(lab):
    ws = _init_workspace(lab)
    stray = ws / "other-clone"
    stray.mkdir()
    lab.git(stray, "init")

    warnings = _session(ws).warnings

    assert any("stray" in w and "other-clone" in w for w in warnings)


def test_a_second_worktree_of_the_bare_is_reported_as_a_stray(lab):
    ws = _init_workspace(lab)
    lab.git(repos_dir() / "community.git", "worktree", "add", "--detach",
            str(ws / "community-review"), "origin/18.0")

    warnings = _session(ws).warnings

    assert any("stray" in w and "community-review" in w for w in warnings)


def test_relative_worktree_gitdir_is_not_prunable(lab):
    """A worktree whose admin dir stores a relative gitdir is healthy, not prunable."""
    root = lab.home / "ws"
    worktree = root / "community"
    worktree.mkdir(parents=True)
    admin = repos_dir() / "community.git" / "worktrees" / "community"
    admin.mkdir(parents=True)

    (worktree / ".git").write_text(f"gitdir: {admin}\n")
    relative = os.path.relpath(worktree / ".git", admin)
    (admin / "gitdir").write_text(f"{relative}\n")
    (admin / "HEAD").write_text("ref: refs/heads/18.0\n")

    locations = derive_repos(root)

    assert [loc.name for loc in locations] == ["community"]
    assert locations[0].problem is None


def test_legacy_keys_and_rejected_bases_collapse_to_one_warning(lab):
    ws = _init_workspace(lab)
    (ws / ".ow" / "config.toml").write_text(
        'version = 2\n\n[repos]\ncommunity = "x..y"\nextra = "also..bad"\n'
    )

    warnings = [w for w in _session(ws).warnings if ".ow/config.toml" in w]

    assert len(warnings) == 1
    assert "older ow format (version)" in warnings[0]
    assert "run `ow init` to migrate it" in warnings[0]
    assert "x..y" in warnings[0]
    assert "also..bad" in warnings[0]


def test_symlinked_stray_checkout_does_not_crash_open_session(lab):
    ws = _init_workspace(lab)
    outside = lab.home / "outside-repo"
    outside.mkdir()
    lab.git(outside, "init")
    (ws / "linked-repo").symlink_to(outside)

    warnings = _session(ws).warnings

    assert any("stray" in w and "linked-repo" in w for w in warnings)


def test_plain_subdir_matching_indexed_workspace_name_falls_through_to_index(lab):
    ws = _init_workspace(lab)
    caller = lab.home / "caller"
    (caller / "ws").mkdir(parents=True)

    result = lab.ow("cd", "ws", cwd=caller)

    assert result.exit_code == 0
    assert result.stdout.strip() == str(ws.resolve())


def test_index_removes_workspace_indexed_through_symlinked_parent(lab):
    from ow.core import index
    from ow.core.paths import index_file

    real_parent = lab.home / "real-parent"
    real_parent.mkdir(parents=True)
    link_parent = lab.home / "link-parent"
    link_parent.symlink_to(real_parent)

    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    ws = init_workspace(lab, "community", root=real_parent / "ws")

    # Simulate an index file storing the non-resolved symlink path
    index_file().write_text(f"{link_parent / 'ws'}\n", encoding="utf-8")

    res = lab.ow("rm", "ws", "-y")
    assert res.exit_code == 0, res.stderr
    assert not ws.exists()
    assert index.read() == ()
