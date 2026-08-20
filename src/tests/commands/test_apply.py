from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_apply
from ow.utils import index
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config


class TestCmdApply:

    def test_cmd_apply_applies_templates(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        repo = ws_dir / "community"
        repo.mkdir()
        (repo / "odoo-bin").touch()
        (repo / "addons").mkdir()
        (repo / "odoo" / "addons").mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        config = config_with_remotes
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})):
                with patch("ow.commands.apply.apply_templates") as mock_apply:
                    cmd_apply(config)
        mock_apply.assert_called_once()

    def test_cmd_apply_with_a_remembered_workspace_name(self, tmp_path, capsys, config_with_remotes):
        """A name the index knows resolves, from any cwd."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        index.remember(ws_dir)
        config = config_with_remotes
        with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})):
            with patch("ow.commands.apply.apply_templates") as mock_apply:
                cmd_apply(config, workspace="test")
        assert mock_apply.call_args.args[2] == ws_dir.resolve()

    def test_cmd_apply_with_workspace_name_not_found(self, tmp_path, capsys, config):
        """A workspace on disk but absent from the index is not a name: no
        silent fallback to a relative path."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        with pytest.raises(SystemExit):
            cmd_apply(config, workspace="nonexistent")
        err = capsys.readouterr().err
        assert "no workspace named 'nonexistent'" in err
        assert "ow ls" in err


class TestCmdApplyOnly:
    """--only narrows which repos are materialised, and nothing else."""

    def _workspace(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master"),
                "enterprise": BranchSpec("origin/master"),
            },
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        return ws_dir

    def test_only_materialises_just_the_named_repo(self, tmp_path, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})) as materialize:
                with patch("ow.commands.apply.apply_templates"):
                    cmd_apply(config_with_remotes, only="community")

        narrowed = materialize.call_args.args[0]
        assert list(narrowed.repos) == ["community"]

    def test_templates_still_see_every_repo(self, tmp_path, config_with_remotes):
        """Rendering a partial config would silently break odoo.conf's addons_path."""
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})):
                with patch("ow.commands.apply.apply_templates") as templates:
                    cmd_apply(config_with_remotes, only="community")

        rendered = templates.call_args.args[0]
        assert list(rendered.repos) == ["community", "enterprise"]

    def test_unknown_alias_fails_and_lists_the_valid_ones(self, tmp_path, config_with_remotes):
        import typer

        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized") as materialize:
                with pytest.raises(typer.BadParameter) as exc:
                    cmd_apply(config_with_remotes, only="nope")

        assert "nope" in str(exc.value)
        assert "community" in str(exc.value)
        assert "enterprise" in str(exc.value)
        materialize.assert_not_called()

    def test_without_only_every_repo_is_materialised(self, tmp_path, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, set(), {})) as materialize:
                with patch("ow.commands.apply.apply_templates"):
                    cmd_apply(config_with_remotes)

        assert list(materialize.call_args.args[0].repos) == ["community", "enterprise"]
