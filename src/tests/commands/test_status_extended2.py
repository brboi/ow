from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.commands.status import _StatusResult, _gather_repo_status, cmd_status
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config
from ow.utils.refs import FetchOutcome


class TestCmdStatusErrorPaths:
    def test_resolve_error(self, tmp_path, capsys, config):
        """When fetch_workspace_refs returns None for a repo, shows error."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.commands.status.fetch_workspace_refs",
                  return_value=FetchOutcome(
                      tracks={"community": "origin/master"}, upstreams={}, specs={}, upstream_before={},
                  )),
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
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = FetchOutcome(
            tracks={"community": "origin/master"}, upstreams={},
            specs={"community": resolved}, upstream_before={},
        )
        def mock_exec(tasks):
            return {"community": RuntimeError("boom")}
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.status.parallel_per_repo", side_effect=mock_exec),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        captured = capsys.readouterr()
        assert "(error)" in captured.out

    def test_github_links_displayed(self, tmp_path, capsys, config):
        """When detached worktree has GitHub link, it is shown."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = FetchOutcome(
            tracks={"community": "origin/master"}, upstreams={},
            specs={"community": resolved}, upstream_before={},
        )
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
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

    def test_no_empty_links_header_when_no_links(self, tmp_path, capsys, config):
        """The links heading must not appear when there is nothing under it."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = FetchOutcome(
            tracks={"community": "origin/master"}, upstreams={},
            specs={"community": resolved}, upstream_before={},
        )
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_worktree_head", return_value=("abc123", "")),
            # A non-GitHub remote produces no links at all.
            patch("ow.commands.status.get_remote_url", return_value="file:///srv/mirrors/odoo.git"),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        captured = capsys.readouterr()
        assert "links" not in captured.out

    def test_status_survives_brackets_in_alias(self, tmp_path, capsys, config):
        """An alias containing Rich markup characters must not crash status."""
        alias = "[/]evil"
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={alias: BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        fetch_return = FetchOutcome(
            tracks={alias: "origin/master"}, upstreams={},
            specs={}, upstream_before={},
        )
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        captured = capsys.readouterr()
        assert alias in captured.out
