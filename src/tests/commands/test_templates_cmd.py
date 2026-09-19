"""`ow templates`: the files ow manages in a workspace, and their state.

Every test here writes under the XDG directories, so every test takes the
`xdg` fixture — directly, or through `config`/`workspace_dir`, which
require it.

The listing and the diff are observed on a real render — the packaged
`common` bundle plus a local bundle of this file's own — because what
`--diff` answers turns on states only a render produces: a file that is not
there yet, a file of bytes no line diff can describe, a template that
renders to nothing. `nothing to render.` is the one line that needs no
test: `common` always renders, so a workspace with no state at all does not
exist; `--diff`'s counterpart (`nothing differs from what ow would write.`)
is reached for real, with a rendered workspace, below.
"""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ow.__main__ import app
from ow.commands.templates import cmd_templates
from ow.utils import paths
from ow.utils.config import Config, WorkspaceConfig, write_workspace_config
from ow.utils.templates import NOT_RENDERED, UP_TO_DATE, apply_templates

runner = CliRunner()

# The local bundle every rendering test declares next to the packaged
# `common` one: one output per shape --diff has to answer for. A bundle's
# files render at the bundle root, so these land at the workspace root.
BUNDLE = "probe"
NOTES = "notes.txt"
BLOB = "blob.bin"
BLANK = "blank.conf"

# Bytes no utf-8 decode accepts: a workspace file ow cannot read as text is
# exactly the shape `--diff` used to crash on.
BLOB_BYTES = b"\x00\x01\xfe\xffow\n"


def make_local_bundle() -> None:
    """Write the bundle under `$XDG_CONFIG_HOME/ow/templates`.

    A plain file in a bundle is taken byte for byte, so a binary template is
    a template like any other — a bundle of your own may well ship one, and
    `--diff` has to answer for it.
    """
    bundle = paths.templates_dir() / BUNDLE
    bundle.mkdir(parents=True)
    (bundle / "notes.txt.j2").write_text("hello {{ ws_name }}\n")
    (bundle / "blob.bin").write_bytes(BLOB_BYTES)
    (bundle / "blank.conf.j2").write_text("   \n")


def make_workspace(tmp_path: Path) -> Path:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True)
    ws = WorkspaceConfig(repos={}, templates=[BUNDLE])
    write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
    return ws_dir


def render(ws_dir: Path, config: Config) -> None:
    """What `ow apply` does to the workspace, without the command layer."""
    apply_templates(WorkspaceConfig(repos={}, templates=[BUNDLE]), config, ws_dir)


def listed(out: str) -> dict[str, str]:
    """The `ow templates` listing, as path -> state."""
    states: dict[str, str] = {}
    for line in out.splitlines():
        if line.strip():
            path, state = line.split(maxsplit=1)
            states[path] = state.strip()
    return states


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

    def test_reports_the_real_state_of_every_rendered_file(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)

        cmd_templates(config, workspace=str(ws_dir))

        states = listed(capsys.readouterr().out)
        assert states["mise/conf.d/00-ow.toml"] == UP_TO_DATE
        assert states[NOTES] == UP_TO_DATE
        assert states[BLOB] == UP_TO_DATE
        assert states[BLANK] == NOT_RENDERED


class TestDiff:

    def test_an_absent_file_is_diffed_as_an_addition(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert "--- /dev/null" in out
        assert f"+++ {NOTES} (ow)" in out
        assert f"+hello {ws_dir.name}" in out
        # The packaged bundle's file is an addition like any other.
        assert "+++ mise/conf.d/00-ow.toml (ow)" in out

    def test_an_absent_binary_file_is_named_rather_than_diffed(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert f"{BLOB}: binary file, ow would write it (absent)" in out
        assert f"+++ {BLOB} (ow)" not in out

    def test_a_whitespace_only_render_is_not_a_change(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        assert BLANK not in capsys.readouterr().out
        assert not (ws_dir / BLANK).exists(), "nothing was rendered, so nothing is proposed"

    def test_a_retired_output_is_not_proposed_as_a_change(self, xdg, tmp_path, config, capsys):
        """ow stopped rendering the file; it stays where it is, and --diff says nothing of it."""
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)
        (paths.templates_dir() / BUNDLE / "notes.txt.j2").unlink()
        render(ws_dir, config)

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert out.strip() == "nothing differs from what ow would write."
        assert (ws_dir / NOTES).read_text() == f"hello {ws_dir.name}\n", "never deleted, never rewritten"

    def test_nothing_to_change_is_stated_not_silent(self, xdg, tmp_path, config, capsys):
        """Silence is indistinguishable from a command that did not run."""
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert out.strip() == "nothing differs from what ow would write."

    def test_a_deleted_file_comes_back_as_an_addition(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)
        (ws_dir / NOTES).unlink()

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert "--- /dev/null" in out
        assert f"+++ {NOTES} (ow)" in out
        assert f"+hello {ws_dir.name}" in out

    def test_a_file_the_user_edited_is_diffed_from_yours_to_ow(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)
        (ws_dir / NOTES).write_text("mine\n")

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert f"--- {NOTES} (yours)" in out
        assert f"+++ {NOTES} (ow)" in out
        assert "-mine" in out
        assert f"+hello {ws_dir.name}" in out

    def test_a_file_ow_moved_on_from_is_diffed_without_the_user_in_it(self, xdg, tmp_path, config, capsys):
        """The user never touched the file: what shows is ow's own change."""
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)
        (paths.templates_dir() / BUNDLE / "notes.txt.j2").write_text("hello again {{ ws_name }}\n")

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert f"--- {NOTES} (yours)" in out
        assert f"-hello {ws_dir.name}" in out
        assert f"+hello again {ws_dir.name}" in out

    def test_a_binary_file_the_user_edited_is_named_rather_than_diffed(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)
        (ws_dir / BLOB).write_bytes(BLOB_BYTES + b"extra")

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert f"{BLOB}: binary file, differs from yours" in out
        assert f"+++ {BLOB} (ow)" not in out

    def test_a_text_file_of_the_user_turned_binary_is_named_rather_than_diffed(self, xdg, tmp_path, config, capsys):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()
        render(ws_dir, config)
        (ws_dir / NOTES).write_bytes(BLOB_BYTES)

        cmd_templates(config, workspace=str(ws_dir), show_diff=True)

        out = capsys.readouterr().out
        assert f"{NOTES}: yours is not text, ow would write text" in out
        assert f"+++ {NOTES} (ow)" not in out


class TestCli:

    def test_diffing_a_real_workspace_exits_zero(self, xdg, tmp_path):
        ws_dir = make_workspace(tmp_path)
        make_local_bundle()

        result = runner.invoke(app, ["templates", "--diff", str(ws_dir)])

        assert result.exit_code == 0, result.output
        assert "--- /dev/null" in result.output
        assert f"+++ {NOTES} (ow)" in result.output