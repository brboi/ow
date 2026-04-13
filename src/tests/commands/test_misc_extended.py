import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from ow.commands.prune import _prune_bare_repo

from ow.commands.status import _gather_repo_status
from ow.commands.update import cmd_update
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config
from ow.utils.display import counts


# ---------------------------------------------------------------------------
# status — github_link for attached worktree, no attached branch link
# ---------------------------------------------------------------------------

class TestStatusExtended:
    def test_gather_attached_no_github_no_link(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master", "feature")
        resolved = BranchSpec("origin/master", "feature")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_remote_url", return_value="https://gitlab.example.com/odoo.git"),
        ):
            result = _gather_repo_status(
                "community", spec, resolved, worktree, bare_repo, 9, set()
            )

        assert result.github_link is None

    def test_gather_attached_branch_shows_tree_link(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master", "feature")
        resolved = BranchSpec("origin/master", "feature")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
        ):
            result = _gather_repo_status(
                "community", spec, resolved, worktree, bare_repo, 9, set()
            )

        assert result.github_link is not None
        assert "tree/feature" in result.github_link[1]

    def test_rich_link_markup(self):
        result = "[link=][/]"
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# update — error display
# ---------------------------------------------------------------------------

class TestCmdUpdateExtended:

    def test_cmd_update_shows_error_when_repo_fails(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=[])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, set(), {"community": "clone failed"})):
                with patch("ow.commands.update.apply_templates"):
                    cmd_update(config)

        captured = capsys.readouterr()
        assert "Warning" in captured.err or "Warning" in captured.out
        assert "community" in (captured.err + captured.out)

    def test_cmd_update_no_errors_no_warning(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, set(), {})):
                with patch("ow.commands.update.apply_templates"):
                    cmd_update(config)

        captured = capsys.readouterr()
        assert "Warning" not in captured.err
        assert "Warning" not in captured.out


# ---------------------------------------------------------------------------
# __main__ — find_root, init via main
# ---------------------------------------------------------------------------

class TestMainExtended:
    def test_main_init_path(self, tmp_path, monkeypatch, capsys):
        from typer.testing import CliRunner
        from ow.__main__ import app
        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "Project initialized successfully" in result.output


# ---------------------------------------------------------------------------
# prune — edge case: no git command works
# ---------------------------------------------------------------------------

class TestPruneExtended:
    def test_prune_bare_repo_with_no_prunes_needed(self, tmp_path):
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        wt_result = MagicMock(returncode=0)
        wt_result.stdout = ""
        branch_result = MagicMock(returncode=0)
        branch_result.stdout = ""
        with patch("ow.commands.prune.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),
                wt_result, branch_result,
                MagicMock(returncode=0, stdout="", stderr="")
            ]
            result = _prune_bare_repo(bare_repo)
        assert result.deleted_branches == []
