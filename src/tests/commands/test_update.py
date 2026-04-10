from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_update
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config


class TestCmdUpdate:

    def test_cmd_update_applies_templates(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        repo = ws_dir / "community"
        repo.mkdir()
        (repo / "odoo-bin").touch()
        (repo / "addons").mkdir()
        (repo / "odoo" / "addons").mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})):
                with patch("ow.commands.update.apply_templates") as mock_apply:
                    cmd_update(config)
        mock_apply.assert_called_once()

    def test_cmd_update_with_workspace_name(self, tmp_path, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.update.ensure_workspace_materialized", return_value=(ws_dir, set(), {})):
                with patch("ow.commands.update.apply_templates") as mock_apply:
                    cmd_update(config, workspace="test")
        mock_apply.assert_called_once()

    def test_cmd_update_with_workspace_name_not_found(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        with pytest.raises(SystemExit):
            cmd_update(config, workspace="nonexistent")
