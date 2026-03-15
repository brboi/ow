from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ow.config import BranchSpec, Config, RemoteConfig, WorkspaceConfig
from ow.workspace import cmd_rebase, cmd_remove, cmd_status


def make_config(
    workspaces=None,
    root_dir=None,
    remotes=None,
) -> Config:
    return Config(
        vars={},
        remotes=remotes or {},
        workspaces=workspaces or [],
        root_dir=root_dir or Path("/root"),
    )


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_cmd_status_drift_exits(tmp_path):
    """cmd_status aborts when drift is detected."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    with patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"):
        with pytest.raises(SystemExit):
            cmd_status(config, "test")


def test_cmd_status_fetches_before_display(tmp_path):
    """cmd_status fetches track branch before displaying status."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    fetch_calls = []

    def track_subprocess_run(args, **kwargs):
        if "fetch" in args:
            fetch_calls.append(args)
        mock = MagicMock(returncode=0)
        mock.stdout = "0\t0\n"
        return mock

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),  # detached = no drift
        patch("ow.workspace.resolve_spec_local", return_value=BranchSpec("origin/master")),
        patch("ow.workspace.subprocess.run", side_effect=track_subprocess_run),
        patch("ow.git.subprocess.run", side_effect=track_subprocess_run),
    ):
        cmd_status(config, "test")

    # At least one fetch should have been called
    assert any("fetch" in c for c in fetch_calls)


# ---------------------------------------------------------------------------
# cmd_rebase
# ---------------------------------------------------------------------------

def test_cmd_rebase_drift_exits(tmp_path):
    """cmd_rebase aborts when drift is detected."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    with patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"):
        with pytest.raises(SystemExit):
            cmd_rebase(config, "test")


def test_cmd_rebase_detached_switches(tmp_path):
    """Detached repos get switch --detach to latest track ref."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    switch_calls = []

    def track_run(args, **kwargs):
        if "switch" in args:
            switch_calls.append(args)
        return MagicMock(returncode=0)

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),  # detached = aligned
        # Detached: only one resolve_spec call (track_spec)
        patch("ow.workspace.resolve_spec", return_value=BranchSpec("origin/master")),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
    ):
        cmd_rebase(config, "test")

    assert any("--detach" in c for c in switch_calls)


def test_cmd_rebase_two_step_rebase(tmp_path):
    """When work branch is pushed to a remote, rebase onto both upstream and track."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    rebase_targets = []

    def track_run(args, **kwargs):
        if "rebase" in args:
            rebase_targets.append(args[-1])
        return MagicMock(returncode=0)

    # resolve_spec called twice: track_spec (detached) then full spec (attached)
    def mock_resolve(bare_repo, spec, remotes):
        if spec.is_detached:
            return BranchSpec("origin/master")
        # Full spec: work branch found on dev
        return BranchSpec("dev/my-feature", "my-feature")

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
    ):
        cmd_rebase(config, "test")

    # Step 1: rebase onto upstream (pushed work branch), Step 2: rebase onto track ref
    assert rebase_targets == ["dev/my-feature", "origin/master"]


def test_cmd_rebase_conflict_reports_and_continues(tmp_path, capsys):
    """On conflict, report and continue to other repos."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (ws_dir / "enterprise").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    (bare_repos_dir / "community.git").mkdir(parents=True)
    (bare_repos_dir / "enterprise.git").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={
            "community": BranchSpec("origin/master", "my-feature"),
            "enterprise": BranchSpec("origin/master", "my-feature"),
        },
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    call_count = {"rebase": 0}

    def track_run(args, **kwargs):
        if "rebase" in args:
            call_count["rebase"] += 1
            if call_count["rebase"] == 1:
                # First rebase (community) fails
                return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    # No upstream (work branch not pushed) — only track branch rebase
    def mock_resolve(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
    ):
        with pytest.raises(SystemExit):
            cmd_rebase(config, "test")

    captured = capsys.readouterr()
    assert "CONFLICT" in captured.err
    # Enterprise rebase was still attempted (rebase count > 1)
    assert call_count["rebase"] >= 2


def test_cmd_rebase_no_upstream_when_not_pushed(tmp_path):
    """When work branch is not on any remote, only rebase onto track."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    (bare_repos_dir / "community.git").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    rebase_targets = []

    def track_run(args, **kwargs):
        if "rebase" in args:
            rebase_targets.append(args[-1])
        return MagicMock(returncode=0)

    # resolve_spec returns same base_ref for both calls → no upstream
    def mock_resolve(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
    ):
        cmd_rebase(config, "test")

    # Only one rebase (onto track_ref), no upstream step
    assert rebase_targets == ["origin/master"]


# ---------------------------------------------------------------------------
# cmd_remove
# ---------------------------------------------------------------------------

def test_cmd_remove_drift_exits(tmp_path):
    """cmd_remove aborts when drift is detected."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / "ow.toml").write_text('[[workspace]]\nname = "test"\nrepo.community = "master..my-feature"\n')
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    with patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"):
        with pytest.raises(SystemExit):
            cmd_remove(config, "test")


def test_cmd_remove_succeeds_when_aligned(tmp_path):
    """cmd_remove proceeds when no drift."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / "ow.toml").write_text('[[workspace]]\nname = "test"\nrepo.community = "master"\n')
    bare_repos_dir = tmp_path / ".bare-git-repos"
    (bare_repos_dir / "community.git").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),  # detached = aligned
        patch("ow.workspace.remove_worktree") as mock_remove,
    ):
        cmd_remove(config, "test")

    mock_remove.assert_called_once()
