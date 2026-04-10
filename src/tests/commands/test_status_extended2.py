from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.commands.status import _StatusResult, _gather_repo_status, cmd_status
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config


class TestCmdStatusErrorPaths:
    def test_resolve_error(self, tmp_path, capsys, config):
        """When fetch_workspace_refs returns None for a repo, shows error."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.commands.status.fetch_workspace_refs",
                  return_value=({"community": "origin/master"}, {}, {})),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        captured = capsys.readouterr()
        assert "error" in captured.out.lower()

    def test_task_exception_shows_error(self, tmp_path, capsys, config):
        """When parallel task raises exception, shows (error)."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = ({"community": "origin/master"}, {}, {"community": resolved})
        def mock_exec(tasks):
            return {"community": RuntimeError("boom")}
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.status.parallel_per_repo", side_effect=mock_exec),
            patch("ow.commands.status.warn_if_drifted"),
            patch("ow.commands.status.get_all_remote_refs", return_value={"origin/master"}),
        ):
            cmd_status(config)
        captured = capsys.readouterr()
        assert "(error)" in captured.out

    def test_github_links_displayed(self, tmp_path, capsys, config):
        """When detached worktree has GitHub link, it is shown."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = ({"community": "origin/master"}, {}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.status.get_all_remote_refs", return_value={"origin/master"}),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_worktree_head", return_value=("abc123", "")),
            patch("ow.commands.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        captured = capsys.readouterr()
        assert "github.com" in captured.out
        # runbot only for attached, check github link displayed
        assert "github.com" in captured.out
