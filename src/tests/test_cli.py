import re
import tomllib
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ow.__main__ import app
from ow.utils import paths
from ow.utils.config import BranchSpec, WorkspaceConfig, write_workspace_config

runner = CliRunner()


def test_no_args_shows_help():
    """ow without args shows help (no_args_is_help=True)."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Odoo workspace manager" in result.output


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(flag):
    """The two spellings 2.0 freezes."""
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
    assert result.output.startswith("ow ")


def test_short_v_is_not_version():
    """1.x spelled it `-v`; 2.0 does not.

    `-v` is near-universally --verbose, and ow shells out to git constantly,
    so a verbosity flag is the obvious addition. Binding it to --version
    forecloses that permanently, and the CLI surface freezes at 2.0 — so the
    short spelling is dropped now, while it still can be. This test is the
    reservation: `-v` must stay unclaimed by --version.
    """
    result = runner.invoke(app, ["-v"])

    assert result.exit_code == 2
    assert not result.output.startswith("ow ")


def test_init_with_args(xdg):
    """ow init myws -r community:master..x -t common calls cmd_init with correct args."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, [
            "init",
            "myws",
            "-r", "community:master..x",
            "-t", "common",
        ])

    assert result.exit_code == 0
    mock_init.assert_called_once()
    call_kwargs = mock_init.call_args
    assert call_kwargs.kwargs["name"] == "myws"
    assert call_kwargs.kwargs["templates"] == ["common"]
    assert "community" in call_kwargs.kwargs["repos"]
    assert call_kwargs.kwargs["repos"]["community"] == BranchSpec("origin/master", "x")


def test_init_without_name_passes_none(xdg):
    """`ow init` with no argument means "here" — the name must reach cmd_init as None."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, ["init", "-r", "community:master..x", "-t", "common"])

    assert result.exit_code == 0
    assert mock_init.call_args.kwargs["name"] is None


def test_init_rejects_repo_without_spec(xdg):
    """-r ALIAS with no ':' must fail loudly, not silently drop the repo."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, ["init", "myws", "-r", "community"])

    assert result.exit_code != 0
    assert "ALIAS:SPEC" in result.output
    assert "community:master..x" in result.output
    mock_init.assert_not_called()


def test_init_rejects_repo_with_empty_alias(xdg):
    """-r :spec has no alias to attach the spec to."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, ["init", "myws", "-r", ":master..x"])

    assert result.exit_code != 0
    assert "ALIAS:SPEC" in result.output
    mock_init.assert_not_called()


def test_init_accepts_several_repos(xdg):
    """-r is repeatable."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, [
            "init", "myws",
            "-r", "community:master..x",
            "-r", "enterprise:master..x",
        ])

    assert result.exit_code == 0
    repos = mock_init.call_args.kwargs["repos"]
    assert sorted(repos) == ["community", "enterprise"]


def test_apply(xdg):
    """ow apply calls cmd_apply."""
    with patch("ow.__main__.cmd_apply", autospec=True) as mock_apply:
        result = runner.invoke(app, ["apply"])

    assert result.exit_code == 0
    mock_apply.assert_called_once()


def test_apply_with_workspace(xdg):
    """ow apply myws calls cmd_apply with workspace="myws"."""
    with patch("ow.__main__.cmd_apply", autospec=True) as mock_apply:
        result = runner.invoke(app, ["apply", "myws"])

    assert result.exit_code == 0
    mock_apply.assert_called_once()
    assert mock_apply.call_args.kwargs["workspace"] == "myws"


def test_apply_only_reaches_cmd_apply(xdg):
    """ow apply --only community,enterprise passes only= through, same as rebase."""
    with patch("ow.__main__.cmd_apply", autospec=True) as mock_apply:
        result = runner.invoke(app, ["apply", "myws", "--only", "community,enterprise"])

    assert result.exit_code == 0
    mock_apply.assert_called_once()
    assert mock_apply.call_args.kwargs["only"] == "community,enterprise"


def test_apply_only_defaults_to_none(xdg):
    """No --only means no narrowing."""
    with patch("ow.__main__.cmd_apply", autospec=True) as mock_apply:
        result = runner.invoke(app, ["apply"])

    assert result.exit_code == 0
    assert mock_apply.call_args.kwargs["only"] is None


def test_apply_only_is_really_wired_to_cmd_apply(tmp_path, xdg, monkeypatch):
    """End to end, without patching cmd_apply.

    The two tests above patch cmd_apply, so they pass even when the CLI passes
    an argument the command does not accept — which is exactly how `--only`
    shipped broken once. This one calls the real function.

    ensure_workspace_materialized is patched because the alias is invalid and
    select_aliases should raise before it is ever called — if a future change
    makes that assumption false, this must fail on a fast, offline mock
    instead of hanging on a real clone of odoo/odoo.
    """
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=[]),
    )
    monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))

    with patch("ow.commands.apply.ensure_workspace_materialized") as materialize:
        result = runner.invoke(app, ["apply", "--only", "nope"])

    assert result.exit_code == 2
    assert "nope" in result.output
    assert "community" in result.output
    materialize.assert_not_called()


@pytest.mark.parametrize("command", ["apply", "status", "rebase"])
def test_workspace_argument_help_names_every_form(command):
    """[WORKSPACE] accepts four forms; the help has to name all four.

    Someone whose workspace is not in the index yet — exactly the state
    right after migrating — reads a help line that says "Workspace name",
    tries the name, and is told to pass a path: a form the help never
    mentioned. This text freezes at 2.0, so it says the whole rule.
    """
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0
    # Rich wraps the help inside a coloured box, so a phrase can be split
    # across two lines with a border between the halves. Drop the escape
    # sequences and the box, then collapse the wrapping, before matching.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    text = " ".join(re.sub(r"[\u2500-\u257f]", " ", plain).split())
    assert "ow ls" in text          # form 1: a name the index knows
    assert "./" in text             # form 2: a path
    assert "OW_WORKSPACE" in text   # form 3: the environment variable
    assert "current directory" in text  # form 4: the cwd walk-up


def test_status_with_workspace(xdg):
    """ow status myws calls cmd_status with workspace="myws"."""
    with patch("ow.__main__.cmd_status", autospec=True) as mock_status:
        result = runner.invoke(app, ["status", "myws"])

    assert result.exit_code == 0
    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] == "myws"


def test_status_without_workspace(xdg):
    """ow status calls cmd_status with workspace=None."""
    with patch("ow.__main__.cmd_status", autospec=True) as mock_status:
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] is None


def test_rebase_with_workspace(xdg):
    """ow rebase myws calls cmd_rebase with workspace="myws"."""
    with patch("ow.__main__.cmd_rebase", autospec=True) as mock_rebase:
        result = runner.invoke(app, ["rebase", "myws"])

    assert result.exit_code == 0
    mock_rebase.assert_called_once()
    assert mock_rebase.call_args.kwargs["workspace"] == "myws"


def test_prune(xdg):
    """ow prune calls cmd_prune."""
    with patch("ow.__main__.cmd_prune", autospec=True) as mock_prune:
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    mock_prune.assert_called_once()


def test_ls(xdg):
    """ow ls wiring: __main__.ls() calls cmd_ls() with no arguments, and does
    not route through _load_config() to get there — ls needs no Config.

    cmd_ls is mocked here, so this is wiring only: it says nothing about
    whether the real cmd_ls detects a legacy layout. That guard
    (check_legacy_layout(), called from inside cmd_ls itself — see
    ow/commands/ls.py) is exercised against the real function in
    src/tests/commands/test_ls.py::test_detects_legacy_layout.
    """
    with patch("ow.__main__.cmd_ls", autospec=True) as mock_ls, \
            patch("ow.__main__._load_config", autospec=True) as mock_load_config:
        result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0
    mock_ls.assert_called_once_with()
    mock_load_config.assert_not_called()


def test_creates_config_if_missing(xdg):
    """If the global config doesn't exist yet, it is bootstrapped with default content."""
    assert not paths.config_file().exists()

    with patch("ow.__main__.cmd_prune", autospec=True):
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    assert paths.config_file().exists()
    content = paths.config_file().read_text()
    assert "community" in content
    assert "origin.url" in content


def test_exits_nonzero_if_config_load_fails(xdg):
    """An unreadable config surfaces as a short message, not a traceback."""
    with patch("ow.__main__.load_global_config", side_effect=OSError("boom")):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "boom" in result.output
    assert str(paths.config_file()) in result.output


def test_exits_nonzero_if_config_is_malformed_toml(xdg):
    """A config.toml that fails to parse is reported the same way, by name."""
    with patch(
        "ow.__main__.load_global_config",
        side_effect=tomllib.TOMLDecodeError("bad toml"),
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "bad toml" in result.output
    assert str(paths.config_file()) in result.output


def test_legacy_layout_is_detected_before_the_config_bootstrap(xdg, tmp_path):
    """The load-bearing test of task 10: the legacy check must run before
    load_global_config() creates a default config.toml.

    If the order were reversed, load_global_config() would bootstrap
    config.toml on this very call — erasing the "no global config yet"
    condition that form 1 of the legacy check depends on — and the command
    would proceed as if nothing were wrong.
    """
    (tmp_path / "ow.toml").write_text("")

    with patch("ow.__main__.cmd_status", autospec=True) as mock_status:
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert "ow.toml" in result.output
    assert "docs/migrating-to-2.0.md" in result.output
    mock_status.assert_not_called()
    assert not paths.config_file().exists()


# ---------------------------------------------------------------------------
# Tab completion
#
# These drive Click's real ShellComplete path rather than calling the callbacks
# with a hand-built context: the callback signature and the return type are
# part of the contract, and a fake context hides breakage in both.
# ---------------------------------------------------------------------------


def _complete(args, incomplete):
    from click.shell_completion import ShellComplete
    from typer.main import get_command

    comp = ShellComplete(get_command(app), {}, "ow", "_OW_COMPLETE")
    return [item.value for item in comp.get_completions(args, incomplete)]


def _make_templates(*names):
    for name in names:
        (paths.templates_dir() / name).mkdir(parents=True)


def _write_remotes(*names):
    body = "".join(f'{n}.origin.url = "git@github.com:odoo/{n}.git"\n' for n in names)
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[remotes]\n" + body)


def test_complete_gen_templates(xdg):
    """Template completion returns correct template names."""
    _make_templates("common", "vscode", "zed")
    names = _complete(["init", "-t"], "")
    assert "common" in names
    assert "vscode" in names
    assert "zed" in names


def test_complete_gen_templates_with_prefix(xdg):
    """Template completion filters by prefix."""
    _make_templates("common", "vscode")
    assert _complete(["init", "-t"], "v") == ["vscode"]


def test_complete_gen_templates_none_taken(xdg):
    """Template completion still offers the packaged templates when nothing local exists."""
    names = _complete(["init", "-t"], "")
    assert "common" in names
    assert "vscode" in names


def test_complete_gen_repos(xdg):
    """Repo completion returns unused aliases."""
    _write_remotes("community", "enterprise")
    names = _complete(["init", "-r"], "")
    assert "community" in names
    assert "enterprise" in names


def test_complete_gen_repos_excludes_used(xdg):
    """Repo completion excludes aliases already given on the command line."""
    _write_remotes("community", "enterprise")
    names = _complete(["init", "-r", "community:master", "-r"], "")
    assert "community" not in names
    assert "enterprise" in names


def test_complete_gen_repos_with_prefix(xdg):
    """Repo completion filters by prefix."""
    _write_remotes("community", "enterprise")
    assert _complete(["init", "-r"], "e") == ["enterprise"]


def test_complete_gen_repos_no_config_creates_nothing(xdg):
    """Completion must never bootstrap: with no config yet, it offers nothing
    rather than create one.

    A real `ow init -r <TAB>` from an old project root with no global config
    used to create ~/.config/ow/config.toml as a side effect of completion,
    permanently destroying the "no global config yet" condition that
    check_legacy_layout() depends on — so the next real command would print
    "no workspace found" instead of pointing at the migration guide.
    """
    assert not paths.config_file().exists()

    names = _complete(["init", "-r"], "")

    assert names == []
    assert not paths.config_file().exists()


def test_complete_gen_repos_preserves_legacy_detection(xdg, tmp_path):
    """End to end: completing from a legacy layout with no global config must
    not erase the condition the legacy check depends on — the migration
    pointer must still fire on the next real command."""
    (tmp_path / "ow.toml").write_text("")
    assert not paths.config_file().exists()

    _complete(["init", "-r"], "")

    assert not paths.config_file().exists()

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "ow.toml" in result.output
    assert "docs/migrating-to-2.0.md" in result.output


def test_complete_workspace_name_offers_known_workspaces(xdg, tmp_path):
    """Workspace name completion reads the same discovery index `ow ls` reads."""
    from ow.utils import index

    # Two of them share a name: `ow ls` distinguishes them by path, but a
    # completion candidate is only a name, so it is offered once.
    for path in ("canary", "trunk", "elsewhere/canary"):
        ws = tmp_path / path
        (ws / ".ow").mkdir(parents=True)
        (ws / ".ow" / "config.toml").write_text("")
        index.remember(ws)

    assert _complete(["status"], "") == ["canary", "trunk"]
    assert _complete(["status"], "c") == ["canary"]


def test_complete_workspace_name_with_no_index_creates_nothing(xdg):
    """No index yet means no candidates — and completion writes nothing."""
    assert _complete(["status"], "") == []
    assert not paths.index_file().exists()
    assert not paths.config_file().exists()


class TestRebaseFlags:
    def test_flags_reach_cmd_rebase(self):
        from typer.testing import CliRunner
        from ow.__main__ import app
        with patch("ow.__main__.cmd_rebase", autospec=True) as mock, patch("ow.__main__._load_config"):
            CliRunner().invoke(
                app,
                ["rebase", "parrot", "--only", "community,enterprise",
                 "--autostash", "--dry-run", "-y"],
            )
        _, kwargs = mock.call_args
        assert kwargs["workspace"] == "parrot"
        assert kwargs["only"] == "community,enterprise"
        assert kwargs["autostash"] is True
        assert kwargs["dry_run"] is True
        assert kwargs["yes"] is True

    def test_defaults_are_conservative(self):
        from typer.testing import CliRunner
        from ow.__main__ import app
        with patch("ow.__main__.cmd_rebase", autospec=True) as mock, patch("ow.__main__._load_config"):
            CliRunner().invoke(app, ["rebase"])
        _, kwargs = mock.call_args
        assert kwargs["only"] is None
        assert kwargs["autostash"] is False
        assert kwargs["dry_run"] is False
        assert kwargs["yes"] is False


class TestInterrupt:
    def test_ctrl_c_exits_130_with_a_message(self, capsys):
        from unittest.mock import patch
        import pytest
        from ow.__main__ import main

        with (
            patch("ow.__main__.app", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc,
        ):
            main()

        assert exc.value.code == 130
        assert "Interrupted" in capsys.readouterr().err

    def test_a_normal_run_does_not_touch_the_exit_code(self):
        from unittest.mock import patch
        from ow.__main__ import main

        with patch("ow.__main__.app") as mock_app:
            main()
        mock_app.assert_called_once()
