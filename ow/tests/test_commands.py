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

def test_cmd_status_drift_warns(tmp_path, capsys):
    """cmd_status warns when drift is detected but continues."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    def track_subprocess_run(args, **kwargs):
        mock = MagicMock(returncode=0)
        mock.stdout = "0\t0\n"
        return mock

    with (
        patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.workspace.resolve_spec_local", return_value=BranchSpec("origin/master")),
        patch("ow.workspace.subprocess.run", side_effect=track_subprocess_run),
        patch("ow.git.subprocess.run", side_effect=track_subprocess_run),
    ):
        cmd_status(config, "test")

    captured = capsys.readouterr()
    assert "Warning" in captured.err


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
        templates=["common"],
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

    assert any("fetch" in c for c in fetch_calls)


def test_cmd_rebase_drift_warns(tmp_path, capsys):
    """cmd_rebase warns when drift is detected but continues."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    def track_run(args, **kwargs):
        return MagicMock(returncode=0)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.workspace.resolve_spec", return_value=BranchSpec("origin/master")),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
    ):
        cmd_rebase(config, "test")

    captured = capsys.readouterr()
    assert "Warning" in captured.err


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
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    switch_calls = []

    def track_run(args, **kwargs):
        if "switch" in args:
            switch_calls.append(args)
        return MagicMock(returncode=0)

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),
        patch("ow.workspace.resolve_spec", return_value=BranchSpec("origin/master")),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
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
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    rebase_targets = []

    def track_run(args, **kwargs):
        if "rebase" in args:
            rebase_targets.append(args[-1])
        return MagicMock(returncode=0)

    def mock_resolve(bare_repo, spec, remotes):
        if spec.is_detached:
            return BranchSpec("origin/master")
        return BranchSpec("dev/my-feature", "my-feature")

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
    ):
        cmd_rebase(config, "test")

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
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    call_count = {"rebase": 0}

    def track_run(args, **kwargs):
        if "rebase" in args:
            call_count["rebase"] += 1
            if call_count["rebase"] == 1:
                return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    def mock_resolve(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
    ):
        with pytest.raises(SystemExit):
            cmd_rebase(config, "test")

    captured = capsys.readouterr()
    assert "CONFLICT" in captured.err
    assert call_count["rebase"] >= 2


def test_cmd_rebase_no_upstream_when_not_pushed(tmp_path):
    """When work branch is not on any remote, only rebase onto track."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    bare_repos_dir = tmp_path / ".bare-git-repos"
    bare_repo = bare_repos_dir / "community.git"
    bare_repo.mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    rebase_targets = []

    def track_run(args, **kwargs):
        if "rebase" in args:
            rebase_targets.append(args[-1])
        return MagicMock(returncode=0)

    def mock_resolve(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="my-feature"),
        patch("ow.workspace.resolve_spec", side_effect=mock_resolve),
        patch("ow.git.subprocess.run", side_effect=track_run),
        patch("ow.workspace.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
    ):
        cmd_rebase(config, "test")

    assert rebase_targets == ["origin/master"]


# ---------------------------------------------------------------------------
# cmd_remove
# ---------------------------------------------------------------------------

def test_cmd_remove_drift_warns(tmp_path, capsys):
    """cmd_remove warns when drift is detected but continues."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / "ow.toml").write_text('[[workspace]]\nname = "test"\ntemplates = ["common"]\nrepo.community = "master..my-feature"\n')
    bare_repos_dir = tmp_path / ".bare-git-repos"
    (bare_repos_dir / "community.git").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    with (
        patch("ow.workspace.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.workspace.remove_worktree"),
    ):
        cmd_remove(config, "test")

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_remove_succeeds_when_aligned(tmp_path):
    """cmd_remove proceeds when no drift."""
    ws_dir = tmp_path / "workspaces" / "test"
    community_dir = ws_dir / "community"
    community_dir.mkdir(parents=True)
    (community_dir / ".git").write_text("gitdir: ../../.bare-git-repos/community.git/worktrees/community")
    (tmp_path / "ow.toml").write_text('[[workspace]]\nname = "test"\ntemplates = ["common"]\nrepo.community = "master"\n')
    bare_repos_dir = tmp_path / ".bare-git-repos"
    (bare_repos_dir / "community.git").mkdir(parents=True)
    ws = WorkspaceConfig(
        name="test",
        repos={"community": BranchSpec("origin/master")},
        templates=["common"],
    )
    config = make_config(workspaces=[ws], root_dir=tmp_path)

    with (
        patch("ow.workspace.get_worktree_branch", return_value=None),
        patch("ow.workspace.remove_worktree") as mock_remove,
    ):
        cmd_remove(config, "test")

    mock_remove.assert_called_once()
