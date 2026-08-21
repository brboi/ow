from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import subprocess

from ow.commands import cmd_apply
from ow.utils import index
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    load_workspace_config,
    write_workspace_config,
)


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
        with pytest.raises(SystemExit) as exc:
            cmd_apply(config, workspace="nonexistent")
        # Not just "it stopped": sys.exit(0) here would report success for a
        # workspace that was never found.
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "no workspace named 'nonexistent'" in err
        assert "ow ls" in err


class TestCmdApplyFailedRepos:
    """A repo that failed to set up must not be reported as a clean run."""

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

    def test_a_failed_repo_exits_non_zero(self, tmp_path, capsys, config_with_remotes):
        """Otherwise CI goes green on half a workspace."""
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {"enterprise": "unreachable"}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes)

        assert exc.value.code == 1

    def test_the_closing_line_does_not_claim_success(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {"enterprise": "unreachable"}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes)

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert f"Workspace '{ws_dir.name}' applied." not in out
        assert "1 repo failed" in out

    def test_a_clean_run_still_says_applied_and_does_not_exit(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community", "enterprise"}, {}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    cmd_apply(config_with_remotes)

        assert f"Workspace '{ws_dir.name}' applied." in capsys.readouterr().out


class TestCmdApplyVarBackfill:
    """Global vars must NOT be written into the workspace config.

    The render merge already makes globals visible to templates via
    ``{**config.vars, **ws.vars}``, so persisting them into the workspace
    file pins today's global values and prevents later global edits from
    taking effect.
    """

    def _workspace(self, tmp_path, vars):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        write_workspace_config(
            ws_dir / ".ow" / "config.toml",
            WorkspaceConfig(
                repos={"community": BranchSpec("origin/master")},
                templates=["common"],
                vars=vars,
            ),
        )
        return ws_dir

    def _apply(self, ws_dir, config):
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    cmd_apply(config)

    def test_apply_does_not_backfill_global_vars(self, tmp_path, config_with_remotes):
        """A var only the global config knows must not land in the workspace file."""
        ws_dir = self._workspace(tmp_path, {"http_port": 8069})

        self._apply(ws_dir, config_with_remotes)

        written = load_workspace_config(ws_dir / ".ow" / "config.toml")
        # db_host and db_port are in config_with_remotes.vars but NOT in ws.vars;
        # they must NOT be written into the workspace config file.
        assert "db_host" not in written.vars
        assert "db_port" not in written.vars
        # The workspace's own var is untouched.
        assert written.vars["http_port"] == 8069



class TestCmdApplyMiseTrust:
    """mise trust is a convenience, not a condition of the command succeeding."""

    def _workspace(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        (ws_dir / "mise.toml").write_text("[tools]\n")
        return ws_dir

    def test_mise_trust_failure_warns_and_still_exits_non_zero_on_repo_error(
        self, tmp_path, capsys, config_with_remotes
    ):
        ws_dir = self._workspace(tmp_path)
        failure = subprocess.CalledProcessError(1, ["mise", "trust"])

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, set(), {"community": "clone failed"}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    with patch(
                        "ow.commands.apply.run_cmd", side_effect=failure
                    ):
                        with pytest.raises(SystemExit) as exc:
                            cmd_apply(config_with_remotes)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert str(ws_dir / "mise.toml") in err
        assert "mise trust" in err

    def test_mise_trust_failure_warns_and_still_reports_success_when_no_errors(
        self, tmp_path, capsys, config_with_remotes
    ):
        ws_dir = self._workspace(tmp_path)
        failure = subprocess.CalledProcessError(1, ["mise", "trust"])

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    with patch(
                        "ow.commands.apply.run_cmd", side_effect=failure
                    ):
                        cmd_apply(config_with_remotes)

        captured = capsys.readouterr()
        assert "Workspace 'ws' applied." in captured.out
        assert str(ws_dir / "mise.toml") in captured.err
        assert "mise trust" in captured.err

    def test_mise_not_installed_warns_and_carries_on(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {}),
            ):
                with patch("ow.commands.apply.apply_templates"):
                    with patch(
                        "ow.commands.apply.run_cmd", side_effect=FileNotFoundError("mise")
                    ):
                        cmd_apply(config_with_remotes)

        assert "mise trust" in capsys.readouterr().err


class TestCmdApplyCheck:
    """--check reports drift and stale templates without modifying anything."""

    def _workspace(self, tmp_path, *, create_worktrees=True):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        if create_worktrees:
            for alias in ws.repos:
                (ws_dir / alias).mkdir()
        return ws_dir

    def test_check_exits_non_zero_when_drifted(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=True):
                with patch("ow.commands.apply.outdated_templates", return_value=[]):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1

    def test_check_exits_zero_when_clean(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.outdated_templates", return_value=[]):
                    cmd_apply(config_with_remotes, check=True)
        assert "up to date" in capsys.readouterr().out

    def test_check_does_not_materialize_or_render(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.outdated_templates", return_value=[]):
                    with patch("ow.commands.apply.ensure_workspace_materialized") as mock_mat:
                        with patch("ow.commands.apply.apply_templates") as mock_tpl:
                            cmd_apply(config_with_remotes, check=True)
        mock_mat.assert_not_called()
        mock_tpl.assert_not_called()

    def test_check_reports_outdated_templates(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.outdated_templates", return_value=["common/odools.toml.j2"]):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1
        assert "common/odools.toml.j2" in capsys.readouterr().out

    def test_check_exits_nonzero_when_worktree_missing(self, tmp_path, capsys, config_with_remotes):
        """--check exits 1 when a configured repo has no worktree directory."""
        ws_dir = self._workspace(tmp_path, create_worktrees=False)
        # Workspace has repos configured but no worktree directories exist
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.outdated_templates", return_value=[]):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "missing" in err.lower()
        assert "community" in err
