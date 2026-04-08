from pathlib import Path
from unittest.mock import patch

import pytest

from ow.utils.config import BranchSpec, WorkspaceConfig
from ow.utils.drift import DriftResult, check_drift, warn_if_drifted


# ---------------------------------------------------------------------------
# DriftResult
# ---------------------------------------------------------------------------

def test_drift_result_detached_config_detached_worktree():
    """Config says detached, worktree is detached - no drift."""
    dr = DriftResult(
        alias="community", spec=BranchSpec("origin/master"), actual_branch=None
    )
    assert dr.is_drifted is False


def test_drift_result_detached_config_worktree_on_branch():
    """Config says detached, worktree is on a branch - drift."""
    dr = DriftResult(
        alias="community", spec=BranchSpec("origin/master"), actual_branch="some-branch"
    )
    assert dr.is_drifted is True


def test_drift_result_attached_config_correct_branch():
    """Config says branch X, worktree is on branch X - no drift."""
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch="my-feature",
    )
    assert dr.is_drifted is False


def test_drift_result_attached_config_worktree_detached():
    """Config says branch X, worktree is detached - drift."""
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch=None,
    )
    assert dr.is_drifted is True


def test_drift_result_attached_config_wrong_branch():
    """Config says branch X, worktree is on branch Y - drift."""
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch="other-branch",
    )
    assert dr.is_drifted is True


def test_drift_result_message_detached_drift():
    dr = DriftResult(
        alias="community",
        spec=BranchSpec("origin/master"),
        actual_branch="rogue-branch",
    )
    msg = dr.message
    assert "community" in msg
    assert "detached" in msg
    assert "rogue-branch" in msg


def test_drift_result_message_attached_drift():
    dr = DriftResult(
        alias="enterprise",
        spec=BranchSpec("origin/master", "my-feature"),
        actual_branch=None,
    )
    msg = dr.message
    assert "enterprise" in msg
    assert "my-feature" in msg
    assert "detached HEAD" in msg


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------

def test_check_drift_uses_get_worktree_branch(tmp_path):
    worktree_path = tmp_path / "community"
    worktree_path.mkdir()
    spec = BranchSpec("origin/master", "my-feature")

    with patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        result = check_drift(worktree_path, spec, "community")

    assert result.alias == "community"
    assert result.actual_branch == "my-feature"
    assert result.is_drifted is False


def test_check_drift_detects_wrong_branch(tmp_path):
    worktree_path = tmp_path / "community"
    worktree_path.mkdir()
    spec = BranchSpec("origin/master", "my-feature")

    with patch("ow.utils.drift.get_worktree_branch", return_value="other-branch"):
        result = check_drift(worktree_path, spec, "community")

    assert result.is_drifted is True


# ---------------------------------------------------------------------------
# warn_if_drifted
# ---------------------------------------------------------------------------

def test_warn_if_drifted_passes_when_aligned(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )

    with patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        warn_if_drifted(ws, ws_dir)

    captured = capsys.readouterr()
    assert "Warning" not in captured.err


def test_warn_if_drifted_warns_on_drift(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )

    with patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"):
        warn_if_drifted(ws, ws_dir)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_warn_if_drifted_skips_unapplied_repos(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    ws = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "my-feature")},
        templates=["common"],
    )

    warn_if_drifted(ws, ws_dir)

    captured = capsys.readouterr()
    assert "Warning" not in captured.err
