"""`ow templates`: the three states, taking a file, and the baseline diff.

Every test here writes under the XDG directories, so every test takes the
`xdg` fixture — directly, or through `config`, which requires it.
"""

import re
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from ow.__main__ import app
from ow.commands import cmd_apply
from ow.commands.templates import cmd_templates, outdated_templates
from ow.utils import paths
from ow.utils.config import BranchSpec, WorkspaceConfig, write_workspace_config

runner = CliRunner()

# The packaged bundle is read straight from the distribution, not through the
# production helpers, so a broken helper cannot make these tests agree with it.
PACKAGED = Path(str(files("ow"))) / "_static" / "templates"
BUNDLE = "common"
REL = "mise.toml.j2"
NAME = f"{BUNDLE}/{REL}"


def packaged_file(rel: str = REL) -> Path:
    path = PACKAGED / BUNDLE / rel
    assert path.is_file(), f"these tests assume the packaged {BUNDLE}/{rel} exists"
    return path


def take_by_hand(*, working: bytes, baseline: bytes | None) -> tuple[Path, Path]:
    """Put a working copy (and optionally a baseline) in place, without --take."""
    copy = paths.templates_dir() / BUNDLE / REL
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(working)
    base = paths.template_base_dir() / BUNDLE / REL
    if baseline is not None:
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(baseline)
    return copy, base


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

    def test_an_untaken_file_is_listed_packaged(self, xdg, capsys):
        packaged_file()
        cmd_templates()
        assert states(capsys.readouterr().out)[NAME] == "packaged"

    def test_a_taken_file_with_a_current_baseline_is_listed_taken(self, xdg, capsys):
        take_by_hand(working=b"my own edits\n", baseline=packaged_file().read_bytes())
        cmd_templates()
        assert states(capsys.readouterr().out)[NAME] == "taken"

    def test_a_taken_file_whose_packaged_version_moved_is_listed_outdated(self, xdg, capsys):
        take_by_hand(working=b"my own edits\n", baseline=b"what ow shipped back then\n")
        cmd_templates()
        assert states(capsys.readouterr().out)[NAME] == "taken, outdated"

    def test_a_taken_file_without_a_baseline_is_taken_and_never_outdated(self, xdg, capsys):
        """Nothing to compare against: the user's own copy is not a baseline."""
        take_by_hand(working=b"my own edits\n", baseline=None)
        cmd_templates()
        assert states(capsys.readouterr().out)[NAME] == "taken"
        assert outdated_templates() == []

    def test_the_other_files_of_a_bundle_stay_packaged_when_one_is_taken(self, xdg, capsys):
        """Taking a file is not forking a bundle."""
        take_by_hand(working=b"my own edits\n", baseline=b"stale\n")
        cmd_templates()
        listed = states(capsys.readouterr().out)
        assert listed[f"{BUNDLE}/odools.toml.j2"] == "packaged"


class TestTake:

    def test_take_writes_both_the_working_copy_and_the_baseline(self, xdg, capsys):
        original = packaged_file().read_bytes()
        cmd_templates(take=NAME)
        copy = paths.templates_dir() / BUNDLE / REL
        base = paths.template_base_dir() / BUNDLE / REL
        assert copy.read_bytes() == original
        assert base.read_bytes() == original

    def test_take_makes_the_file_listed_taken(self, xdg, capsys):
        cmd_templates(take=NAME)
        capsys.readouterr()
        cmd_templates()
        assert states(capsys.readouterr().out)[NAME] == "taken"

    def test_take_refuses_to_overwrite_an_existing_copy(self, xdg, capsys):
        copy, base = take_by_hand(working=b"my own edits\n", baseline=None)
        with pytest.raises(SystemExit) as exc:
            cmd_templates(take=NAME)
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert f"'{NAME}' is already taken" in err
        assert str(copy) in err
        assert copy.read_bytes() == b"my own edits\n"
        assert not base.exists()

    def test_take_wins_over_diff(self, xdg, capsys):
        """--take and --diff together: the take happens, --diff prints nothing.

        Set up an unrelated outdated file first, so a diff would have
        something to say if it ran — proving take's priority, not just an
        absence of anything to diff.
        """
        other_rel = "odools.toml.j2"
        other_copy = paths.templates_dir() / BUNDLE / other_rel
        other_copy.parent.mkdir(parents=True, exist_ok=True)
        other_copy.write_bytes(b"my own edits\n")
        other_base = paths.template_base_dir() / BUNDLE / other_rel
        other_base.parent.mkdir(parents=True, exist_ok=True)
        other_base.write_bytes(b"what ow shipped back then\n")

        original = packaged_file().read_bytes()
        cmd_templates(take=NAME, show_diff=True)

        out = capsys.readouterr().out
        assert f"Took {NAME}" in out
        assert "@@" not in out
        copy = paths.templates_dir() / BUNDLE / REL
        base = paths.template_base_dir() / BUNDLE / REL
        assert copy.read_bytes() == original
        assert base.read_bytes() == original

    def test_take_refuses_to_overwrite_when_a_baseline_already_exists(self, xdg, capsys):
        """The other refusal branch: a copy AND a baseline both already exist.

        A wrong guard could silently reset the baseline to the current
        packaged file here, destroying the record that answers "did ow
        change this?" for a file the user edited by hand after taking it.
        """
        cmd_templates(take=NAME)
        capsys.readouterr()
        copy = paths.templates_dir() / BUNDLE / REL
        base = paths.template_base_dir() / BUNDLE / REL
        copy.write_bytes(b"my own edits\n")
        # Diverge the baseline from the packaged file, as it would after ow
        # ships an update. A reset-to-packaged bug is only visible this way:
        # if the baseline still equalled packaged, a wrongful reset would
        # write back the very same bytes.
        base.write_bytes(b"what ow shipped back then\n")
        baseline_before = base.read_bytes()

        with pytest.raises(SystemExit) as exc:
            cmd_templates(take=NAME)
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert f"'{NAME}' is already taken" in err
        assert copy.read_bytes() == b"my own edits\n"
        assert base.read_bytes() == baseline_before

    def test_take_rejects_a_path_matching_no_packaged_file(self, xdg, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_templates(take="nosuch/thing.j2")
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "no packaged template file named 'nosuch/thing.j2'" in err
        assert BUNDLE in err
        assert not (paths.templates_dir() / "nosuch").exists()


class TestDiff:

    def _stale_baseline(self) -> str:
        """Baseline whose first line differs from the packaged file's."""
        packaged_text = packaged_file().read_text()
        first, _, rest = packaged_text.partition("\n")
        (paths.template_base_dir() / BUNDLE / REL).parent.mkdir(parents=True, exist_ok=True)
        (paths.template_base_dir() / BUNDLE / REL).write_text(f"line-only-in-baseline\n{rest}")
        copy = paths.templates_dir() / BUNDLE / REL
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text("my own edits\n")
        return first

    def test_diff_shows_the_lines_that_moved_from_baseline_to_packaged(self, xdg, capsys):
        first = self._stale_baseline()
        cmd_templates(show_diff=True)
        out = capsys.readouterr().out
        assert "-line-only-in-baseline" in out
        assert f"+{first}" in out

    def test_diff_names_the_baseline_and_the_packaged_side(self, xdg, capsys):
        self._stale_baseline()
        cmd_templates(show_diff=True)
        out = capsys.readouterr().out
        assert f"--- {NAME} (baseline)" in out
        assert f"+++ {NAME} (packaged)" in out

    def test_diff_reports_that_nothing_is_outdated_instead_of_staying_silent(self, xdg, capsys):
        """Silence is indistinguishable from a command that did not run.

        `ow templates` answers its own empty case; --diff must too.
        """
        cmd_templates(show_diff=True)
        out = capsys.readouterr().out
        assert out.strip(), "--diff must say something when nothing is outdated"
        assert "outdated" in out

    def test_diff_still_reports_nothing_outdated_when_a_file_is_taken_and_current(self, xdg, capsys):
        take_by_hand(working=b"my own edits\n", baseline=packaged_file().read_bytes())
        cmd_templates(show_diff=True)
        assert "outdated" in capsys.readouterr().out

    def test_diff_says_nothing_about_a_taken_file_that_is_still_current(self, xdg, capsys):
        take_by_hand(working=b"my own edits\n", baseline=packaged_file().read_bytes())
        cmd_templates(show_diff=True)
        out = capsys.readouterr().out
        assert "@@" not in out
        assert NAME not in out


class TestOutdatedTemplates:

    def test_outdated_names_the_bundle_and_the_relative_path(self, xdg):
        take_by_hand(working=b"my own edits\n", baseline=b"what ow shipped back then\n")
        assert outdated_templates() == [NAME]

    def test_a_current_taken_file_is_not_outdated(self, xdg):
        take_by_hand(working=b"my own edits\n", baseline=packaged_file().read_bytes())
        assert outdated_templates() == []


class TestApplyReportsOutdated:

    def _workspace(self, tmp_path: Path) -> Path:
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        return ws_dir

    def _run(self, tmp_path, config, outdated: list[str]):
        ws_dir = self._workspace(tmp_path)
        with patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}), \
             patch("ow.commands.apply.ensure_workspace_materialized", return_value=(ws_dir, {"community"}, {})), \
             patch("ow.commands.apply.apply_templates"), \
             patch("ow.commands.apply.outdated_templates", autospec=True, return_value=outdated) as mock:
            cmd_apply(config)
        mock.assert_called_once_with()

    def test_apply_reports_each_outdated_template_on_its_own_line(self, tmp_path, config, capsys):
        self._run(tmp_path, config, ["common/mise.toml.j2", "zed/.zed/settings.json"])
        out = capsys.readouterr().out
        lines = [line.strip() for line in out.splitlines()]
        assert "since you took them" in out
        assert "common/mise.toml.j2" in lines
        assert "zed/.zed/settings.json" in lines

    def test_apply_stays_quiet_when_nothing_is_outdated(self, tmp_path, config, capsys):
        self._run(tmp_path, config, [])
        assert "since you took them" not in capsys.readouterr().out


class TestCli:

    def test_cli_passes_take_and_diff_through(self, xdg):
        with patch("ow.__main__.cmd_templates", autospec=True) as mock:
            result = runner.invoke(app, ["templates", "--take", NAME, "--diff"])
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs == {"take": NAME, "show_diff": True}

    def test_cli_defaults_to_listing(self, xdg):
        with patch("ow.__main__.cmd_templates", autospec=True) as mock:
            result = runner.invoke(app, ["templates"])
        assert result.exit_code == 0, result.output
        assert mock.call_args.kwargs == {"take": None, "show_diff": False}
