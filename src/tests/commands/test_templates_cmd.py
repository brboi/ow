"""`ow templates`: the materialised states, and the outdated diff.

Every test here writes under the XDG directories, so every test takes the
`xdg` fixture — directly, or through `config`/`workspace_dir`, which
require it.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from ow.__main__ import app
from ow.commands.templates import cmd_templates
from ow.utils import paths
from ow.utils.config import WorkspaceConfig, load_workspace_config, write_workspace_config
from ow.utils.templates import WS_TEMPLATES, materialize_templates, outdated_templates

runner = CliRunner()

BUNDLE = "common"
REL = "mise.toml.j2"
NAME = f"{BUNDLE}/{REL}"



def make_workspace(tmp_path: Path, *, templates: list[str] | None = None) -> Path:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True)
    ws = WorkspaceConfig(repos={}, templates=templates or [BUNDLE])
    write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
    return ws_dir


def apply_to(ws_dir: Path) -> WorkspaceConfig:
    """Materialise `ws_dir`'s own config into `.ow/templates`."""
    ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    materialize_templates(ws, ws_dir)
    return ws


def make_outdated_workspace(tmp_path: Path) -> Path:
    """Materialise, then edit the workspace copy and move the source."""
    ws_dir = make_workspace(tmp_path)
    apply_to(ws_dir)
    (ws_dir / WS_TEMPLATES / BUNDLE / REL).write_text("my own edits\n")
    local = paths.templates_dir() / BUNDLE
    local.mkdir(parents=True)
    (local / REL).write_text("what ow ships now\n")
    return ws_dir


def states(output: str) -> dict[str, str]:
    """Parse the listing into {template name: state}."""
    parsed = {}
    for line in output.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) == 2:
            parsed[parts[0]] = parts[1]
    return parsed


class TestLegacy:
    """Same gap as `ow ls`: skipping _load_config() also skips the legacy
    check. Someone mid-migration should get the same answer from every
    command, not a working `ow templates` while `ow status` tells them to
    migrate.
    """

    def test_detects_legacy_layout(self, xdg, tmp_path, capsys):
        (tmp_path / "ow.toml").write_text("")

        with pytest.raises(typer.Exit) as exc:
            cmd_templates()

        assert exc.value.exit_code == 1
        err = capsys.readouterr().err
        assert "docs/migrating-to-2.0.md" in err


class TestListing:

    def test_a_freshly_created_workspace_has_nothing_materialised(self, xdg, tmp_path, capsys):
        """Before the first `ow apply`, there is nothing to list yet."""
        ws_dir = make_workspace(tmp_path)
        cmd_templates(workspace=str(ws_dir))
        out = capsys.readouterr().out
        assert "No templates materialised" in out

    def test_a_materialised_untouched_file_is_listed_up_to_date(self, xdg, tmp_path, capsys):
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        cmd_templates(workspace=str(ws_dir))
        assert states(capsys.readouterr().out)[NAME] == "up to date"

    def test_an_edited_file_with_unchanged_source_is_listed_modified(self, xdg, tmp_path, capsys):
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        (ws_dir / WS_TEMPLATES / BUNDLE / REL).write_text("my own edits\n")

        cmd_templates(workspace=str(ws_dir))

        assert states(capsys.readouterr().out)[NAME] == "modified"

    def test_an_edited_file_whose_source_also_moved_is_listed_outdated(self, xdg, tmp_path, capsys):
        ws_dir = make_outdated_workspace(tmp_path)

        cmd_templates(workspace=str(ws_dir))

        assert states(capsys.readouterr().out)[NAME] == "outdated"

    def test_a_hand_added_file_is_listed_unlocked(self, xdg, tmp_path, capsys):
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        extra = ws_dir / WS_TEMPLATES / BUNDLE / "extra.txt"
        extra.write_text("hand added\n")

        cmd_templates(workspace=str(ws_dir))

        assert states(capsys.readouterr().out)[f"{BUNDLE}/extra.txt"] == "unlocked"

    def test_the_other_files_of_a_bundle_stay_up_to_date_when_one_is_edited(self, xdg, tmp_path, capsys):
        """Editing one file is not forking a bundle."""
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        (ws_dir / WS_TEMPLATES / BUNDLE / REL).write_text("my own edits\n")

        cmd_templates(workspace=str(ws_dir))

        listed = states(capsys.readouterr().out)
        assert listed[f"{BUNDLE}/odools.toml.j2"] == "up to date"


class TestDiff:

    def test_diff_shows_the_lines_that_differ_between_yours_and_ow(self, xdg, tmp_path, capsys):
        ws_dir = make_outdated_workspace(tmp_path)
        cmd_templates(workspace=str(ws_dir), show_diff=True)
        out = capsys.readouterr().out
        assert "-my own edits" in out
        assert "+what ow ships now" in out

    def test_diff_names_the_yours_and_the_ow_side(self, xdg, tmp_path, capsys):
        ws_dir = make_outdated_workspace(tmp_path)
        cmd_templates(workspace=str(ws_dir), show_diff=True)
        out = capsys.readouterr().out
        assert f"--- {NAME} (yours)" in out
        assert f"+++ {NAME} (ow)" in out

    def test_diff_reports_that_nothing_is_outdated_instead_of_staying_silent(self, xdg, tmp_path, capsys):
        """Silence is indistinguishable from a command that did not run.

        `ow templates` answers its own empty case; --diff must too.
        """
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        cmd_templates(workspace=str(ws_dir), show_diff=True)
        out = capsys.readouterr().out
        assert out.strip(), "--diff must say something when nothing is outdated"
        assert "outdated" in out

    def test_diff_says_nothing_about_a_file_that_is_merely_modified_not_outdated(self, xdg, tmp_path, capsys):
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        (ws_dir / WS_TEMPLATES / BUNDLE / REL).write_text("my own edits\n")

        cmd_templates(workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert "@@" not in out
        assert NAME not in out


class TestOutdatedTemplates:

    def test_outdated_names_the_bundle_and_the_relative_path(self, xdg, tmp_path):
        ws_dir = make_outdated_workspace(tmp_path)
        assert outdated_templates(ws_dir) == [NAME]

    def test_a_current_untouched_file_is_not_outdated(self, xdg, tmp_path):
        ws_dir = make_workspace(tmp_path)
        apply_to(ws_dir)
        assert outdated_templates(ws_dir) == []


class TestCli:

    def test_cli_passes_workspace_and_diff_through(self, xdg):
        with patch("ow.__main__.cmd_templates", autospec=True) as mock:
            result = runner.invoke(app, ["templates", "--diff"])
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs.get("show_diff") is True
