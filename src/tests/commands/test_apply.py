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
        assert narrowed.repos == {"community": BranchSpec("origin/master")}

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
    """Global vars are copied into the workspace config, once, without clobbering.

    A workspace config is meant to be self-contained — a template renders
    from it alone — so a var only the global config knows has to land in
    the file. That copy must never win over a value the workspace set for
    itself, which is the whole point of a per-workspace override.
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

    def test_a_missing_global_var_is_written_into_the_workspace_config(
        self, tmp_path, config_with_remotes
    ):
        ws_dir = self._workspace(tmp_path, {"http_port": 8069})

        self._apply(ws_dir, config_with_remotes)

        # Read back from disk: the point is what the file now holds, not
        # what the in-memory object happened to be left holding.
        written = load_workspace_config(ws_dir / ".ow" / "config.toml")
        assert written.vars["db_host"] == "localhost"
        assert written.vars["db_port"] == 5432

    def test_a_var_the_workspace_sets_is_not_overwritten(self, tmp_path, config_with_remotes):
        """The global value is a default, not an authority."""
        ws_dir = self._workspace(tmp_path, {"http_port": 9999})

        self._apply(ws_dir, config_with_remotes)

        written = load_workspace_config(ws_dir / ".ow" / "config.toml")
        assert written.vars["http_port"] == 9999

    def test_nothing_is_written_when_nothing_is_missing(self, tmp_path, config_with_remotes):
        """No missing var, no rewrite — an untouched config keeps its file
        mtime and its formatting."""
        ws_dir = self._workspace(
            tmp_path, {"http_port": 8069, "db_host": "localhost", "db_port": 5432}
        )

        with patch("ow.commands.apply.write_workspace_config") as write:
            self._apply(ws_dir, config_with_remotes)

        write.assert_not_called()


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

    def _workspace(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
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
