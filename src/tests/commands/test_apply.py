from pathlib import Path
from unittest.mock import patch

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
from ow.utils.templates import (
    ABSENT,
    OUTDATED,
    RENDERED_LOCK,
    YOURS,
    RenderedFile,
    RenderResult,
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
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()) as mock_apply:
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
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
            with patch("ow.commands.apply.apply_templates", return_value=RenderResult()) as mock_apply:
                with patch("ow.commands.apply.rendered_states", return_value=[]):
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
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
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
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
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
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
                        cmd_apply(config_with_remotes)

        assert f"Workspace '{ws_dir.name}' applied." in capsys.readouterr().out


class TestCmdApplyVarBackfill:
    """Global vars must NOT be written into the workspace config.

    #40: global vars are only seeds at init time, copied once into ws.vars;
    persisting them into the workspace file on every apply would instead
    pin today's global values and prevent later global edits from ever
    reaching a workspace that already exists.
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
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
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
    """mise trust is a convenience, not a condition of the command succeeding.

    ow no longer writes a single <ws>/mise.toml; it writes fragments under
    <ws>/mise/ (e.g. mise/conf.d/00-ow.toml). Trust follows whatever the
    render reports as a file it manages under that prefix.
    """

    def _workspace(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        return ws_dir

    def _mise_result(self):
        return RenderResult(managed=["mise/conf.d/00-ow.toml"])

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
                with patch("ow.commands.apply.apply_templates", return_value=self._mise_result()):
                    with patch("ow.commands.apply.run_cmd", side_effect=failure):
                        with pytest.raises(SystemExit) as exc:
                            cmd_apply(config_with_remotes)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert str(ws_dir / "mise" / "conf.d" / "00-ow.toml") in err
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
                with patch("ow.commands.apply.apply_templates", return_value=self._mise_result()):
                    with patch("ow.commands.apply.run_cmd", side_effect=failure):
                        cmd_apply(config_with_remotes)

        captured = capsys.readouterr()
        assert "Workspace 'ws' applied." in captured.out
        assert str(ws_dir / "mise" / "conf.d" / "00-ow.toml") in captured.err
        assert "mise trust" in captured.err

    def test_mise_not_installed_warns_and_carries_on(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {}),
            ):
                with patch("ow.commands.apply.apply_templates", return_value=self._mise_result()):
                    with patch(
                        "ow.commands.apply.run_cmd", side_effect=FileNotFoundError("mise")
                    ):
                        cmd_apply(config_with_remotes)

        assert "mise trust" in capsys.readouterr().err

    def test_files_outside_mise_are_never_trusted(self, tmp_path, capsys, config_with_remotes):
        """Only the mise/ prefix is trusted; another managed file must not be."""
        ws_dir = self._workspace(tmp_path)
        result = RenderResult(managed=["odoorc", ".zed/settings.json"])

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch(
                "ow.commands.apply.ensure_workspace_materialized",
                return_value=(ws_dir, {"community"}, {}),
            ):
                with patch("ow.commands.apply.apply_templates", return_value=result):
                    with patch("ow.commands.apply.run_cmd") as mock_run:
                        cmd_apply(config_with_remotes)

        mock_run.assert_not_called()


class TestCmdApplyLegacyMiseWarning:
    """A root mise.toml from a pre-rewrite ow shadows mise/conf.d/00-ow.toml."""

    def _workspace(self, tmp_path):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        return ws_dir

    def test_warns_when_an_older_ow_left_a_root_mise_toml(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        legacy_path = ws_dir / "mise.toml"

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, set(), {})):
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
                        with patch("ow.commands.apply.legacy_mise_toml", return_value=legacy_path):
                            cmd_apply(config_with_remotes)

        err = capsys.readouterr().err
        assert f"warning: {legacy_path} was written by an older ow" in err
        assert "mise/conf.d/00-ow.toml" in err
        assert "mise.local.toml" in err

    def test_does_not_warn_when_there_is_no_legacy_file(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)

        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, set(), {})):
                with patch("ow.commands.apply.apply_templates", return_value=RenderResult()):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
                        with patch("ow.commands.apply.legacy_mise_toml", return_value=None):
                            cmd_apply(config_with_remotes)

        assert "written by an older ow" not in capsys.readouterr().err


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
                with patch("ow.commands.apply.rendered_states", return_value=[]):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1

    def test_check_exits_zero_when_clean(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.rendered_states", return_value=[]):
                    cmd_apply(config_with_remotes, check=True)
        assert "up to date" in capsys.readouterr().out

    def test_check_does_not_materialize_or_render(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.rendered_states", return_value=[]):
                    with patch("ow.commands.apply.ensure_workspace_materialized") as mock_mat:
                        with patch("ow.commands.apply.apply_templates", return_value=RenderResult()) as mock_tpl:
                            cmd_apply(config_with_remotes, check=True)
        mock_mat.assert_not_called()
        mock_tpl.assert_not_called()

    def test_check_reports_outdated_templates(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        stale = [
            RenderedFile(path="common/odools.toml.j2", state=OUTDATED, ow_text="new", your_text="old"),
        ]
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.rendered_states", return_value=stale):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1
        assert "common/odools.toml.j2" in capsys.readouterr().out

    def test_check_reports_absent_templates(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        missing = [
            RenderedFile(path="common/mise/conf.d/00-ow.toml.j2", state=ABSENT, ow_text="new", your_text=None),
        ]
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.rendered_states", return_value=missing):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1
        assert "common/mise/conf.d/00-ow.toml.j2" in capsys.readouterr().out

    def test_check_exits_nonzero_when_worktree_missing(self, tmp_path, capsys, config_with_remotes):
        """--check exits 1 when a configured repo has no worktree directory."""
        ws_dir = self._workspace(tmp_path, create_worktrees=False)
        # Workspace has repos configured but no worktree directories exist
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.rendered_states", return_value=[]):
                    with pytest.raises(SystemExit) as exc:
                        cmd_apply(config_with_remotes, check=True)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "missing" in err.lower()
        assert "community" in err

    def test_check_does_not_fail_on_files_the_user_edited(self, tmp_path, capsys, config_with_remotes):
        """YOURS is a stable, intended state, not drift: --check must stay green."""
        ws_dir = self._workspace(tmp_path)
        yours = [
            RenderedFile(path="mise/conf.d/00-ow.toml", state=YOURS, ow_text="ow version", your_text="my version"),
        ]
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.warn_if_drifted", return_value=False):
                with patch("ow.commands.apply.rendered_states", return_value=yours):
                    cmd_apply(config_with_remotes, check=True)
        out = capsys.readouterr().out
        assert "up to date" in out
        assert "yours, left alone: mise/conf.d/00-ow.toml" in out


class TestCmdApplyRenderReport:
    """apply reports what apply_templates did, via the RenderResult it returns."""

    def _workspace(self, tmp_path: Path) -> Path:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        return ws_dir

    def _apply(self, ws_dir, config, result):
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})):
                with patch("ow.commands.apply.apply_templates", return_value=result):
                    with patch("ow.commands.apply.rendered_states", return_value=[]):
                        cmd_apply(config)

    def test_reports_each_written_file(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        self._apply(ws_dir, config_with_remotes, RenderResult(wrote=["common/odoorc.j2"]))
        assert "wrote common/odoorc.j2" in capsys.readouterr().out

    def test_reports_each_updated_file(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        self._apply(ws_dir, config_with_remotes, RenderResult(updated=["common/mise.toml.j2"]))
        assert "updated common/mise.toml.j2" in capsys.readouterr().out

    def test_reports_files_the_user_edited_and_points_at_diff(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        self._apply(ws_dir, config_with_remotes, RenderResult(yours=["common/odoorc.j2"]))
        out = capsys.readouterr().out
        assert "yours, left alone: common/odoorc.j2" in out
        assert "run `ow templates --diff` to see what ow would write instead." in out

    def test_reports_files_that_rendered_blank(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        self._apply(ws_dir, config_with_remotes, RenderResult(skipped=["vscode/launch.json.j2"]))
        assert "not rendered (empty): vscode/launch.json.j2" in capsys.readouterr().out

    def test_stays_quiet_about_categories_that_are_empty(self, tmp_path, capsys, config_with_remotes):
        ws_dir = self._workspace(tmp_path)
        self._apply(ws_dir, config_with_remotes, RenderResult())
        out = capsys.readouterr().out
        assert "ow templates --diff" not in out
        assert "not rendered" not in out


class TestCmdApplyCheckNeverMaterializes:
    """--check is read-only: real code path, no mocked template helpers at all."""

    def test_check_never_writes_the_rendered_lock(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)

        # Nothing is rendered yet, so --check reports what it would write and
        # goes red. What it must not do is write anything on the way out.
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}):
            with pytest.raises(SystemExit):
                cmd_apply(config, check=True)

        assert not (ws_dir / RENDERED_LOCK).exists()
