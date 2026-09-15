"""`ow templates`: the files ow manages in a workspace, and their state.

Every test here writes under the XDG directories, so every test takes the
`xdg` fixture — directly, or through `config`/`workspace_dir`, which
require it.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from ow.__main__ import app
from ow.commands.templates import cmd_templates
from ow.utils.config import WorkspaceConfig, write_workspace_config
from ow.utils.templates import OUTDATED, UP_TO_DATE, YOURS, RenderedFile

runner = CliRunner()


def make_workspace(tmp_path: Path) -> Path:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True)
    ws = WorkspaceConfig(repos={}, templates=[])
    write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
    return ws_dir


class TestLegacy:
    """Same gap as `ow ls`: skipping _load_config() also skips the legacy
    check. Someone mid-migration should get the same answer from every
    command, not a working `ow templates` while `ow status` tells them to
    migrate.
    """

    def test_detects_legacy_layout(self, xdg, tmp_path, config, capsys):
        (tmp_path / "ow.toml").write_text("")

        with pytest.raises(typer.Exit) as exc:
            cmd_templates(config)

        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "docs/migrating-to-2.0.md" in err


class TestListing:

    def test_nothing_to_render_is_stated_not_silent(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        with patch("ow.commands.templates.rendered_states", return_value=[]):
            cmd_templates(config, workspace=str(ws_dir))
        assert "nothing to render." in capsys.readouterr().out

    def test_listing_prints_path_and_state_for_every_file(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        states = [
            RenderedFile(path="common/odoorc.j2", state=UP_TO_DATE, ow_text="a", your_text="a"),
            RenderedFile(path="mise/conf.d/00-ow.toml", state=YOURS, ow_text="b", your_text="b (mine)"),
        ]
        with patch("ow.commands.templates.rendered_states", return_value=states):
            cmd_templates(config, workspace=str(ws_dir))

        out = capsys.readouterr().out
        assert "common/odoorc.j2" in out
        assert UP_TO_DATE in out
        assert "mise/conf.d/00-ow.toml" in out
        assert YOURS in out

    def test_listing_columns_are_aligned_on_the_widest_path(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        longest = "common/much/longer/path.j2"
        states = [
            RenderedFile(path="a", state=UP_TO_DATE, ow_text="x", your_text="x"),
            RenderedFile(path=longest, state=YOURS, ow_text="x", your_text="y"),
        ]
        with patch("ow.commands.templates.rendered_states", return_value=states):
            cmd_templates(config, workspace=str(ws_dir))

        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        columns = {line.index(state) for line, state in zip(lines, (UP_TO_DATE, YOURS))}
        assert columns == {len(longest) + 2}, "states must start in the same column"


class TestDiff:

    def test_diff_shows_the_lines_that_differ_for_a_file_the_user_edited(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        states = [
            RenderedFile(
                path="mise/conf.d/00-ow.toml",
                state=YOURS,
                ow_text="what ow ships now\n",
                your_text="my own edits\n",
            ),
        ]
        with patch("ow.commands.templates.rendered_states", return_value=states):
            cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert "-my own edits" in out
        assert "+what ow ships now" in out

    def test_diff_names_the_yours_and_the_ow_side(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        states = [
            RenderedFile(
                path="mise/conf.d/00-ow.toml",
                state=OUTDATED,
                ow_text="what ow ships now\n",
                your_text="my own edits\n",
            ),
        ]
        with patch("ow.commands.templates.rendered_states", return_value=states):
            cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert "--- mise/conf.d/00-ow.toml (yours)" in out
        assert "+++ mise/conf.d/00-ow.toml (ow)" in out

    def test_diff_reports_that_nothing_differs_instead_of_staying_silent(self, xdg, tmp_path, config, capsys):
        """Silence is indistinguishable from a command that did not run.

        `ow templates` answers its own empty case; --diff must too.
        """
        ws_dir = make_workspace(tmp_path)
        states = [
            RenderedFile(path="common/odoorc.j2", state=UP_TO_DATE, ow_text="a", your_text="a"),
        ]
        with patch("ow.commands.templates.rendered_states", return_value=states):
            cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert out.strip(), "--diff must say something when nothing differs"
        assert "nothing differs from what ow would write." in out

    def test_diff_says_nothing_about_a_file_that_is_up_to_date(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        states = [
            RenderedFile(path="common/odoorc.j2", state=UP_TO_DATE, ow_text="a", your_text="a"),
            RenderedFile(path="mise/conf.d/00-ow.toml", state=YOURS, ow_text="b", your_text="b (mine)"),
        ]
        with patch("ow.commands.templates.rendered_states", return_value=states):
            cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert "common/odoorc.j2" not in out
        assert "mise/conf.d/00-ow.toml" in out


class TestCli:

    def test_cli_passes_workspace_and_diff_through(self, xdg):
        with patch("ow.__main__.cmd_templates", autospec=True) as mock:
            result = runner.invoke(app, ["templates", "--diff"])
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs.get("show_diff") is True
