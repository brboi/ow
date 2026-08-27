import re
import shutil
import tomllib
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ow.__main__ import app
from ow.utils import paths
from ow.utils.config import BranchSpec

runner = CliRunner()


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _unwrap(help_text: str) -> str:
    """Flatten Rich's help panel so a help string can be matched whole.

    Rich boxes and wraps --help, so a one-line help string arrives split
    across rows with colour codes and border glyphs in between. Strip both,
    then collapse the whitespace.
    """
    plain = _ANSI.sub("", help_text)
    plain = re.sub(r"[│┃╭╮╰╯─━]", " ", plain)
    return " ".join(plain.split())


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
    assert mock_prune.call_args.kwargs == {"dry_run": False, "yes": False, "also_backups": False}


def test_prune_dry_run(xdg):
    """--dry-run reaches cmd_prune, or the survey-only stop is unreachable."""
    with patch("ow.__main__.cmd_prune", autospec=True) as mock_prune:
        result = runner.invoke(app, ["prune", "--dry-run"])

    assert result.exit_code == 0
    assert mock_prune.call_args.kwargs["dry_run"] is True


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_prune_yes(xdg, flag):
    """Both spellings rebase accepts, so the two commands read the same."""
    with patch("ow.__main__.cmd_prune", autospec=True) as mock_prune:
        result = runner.invoke(app, ["prune", flag])

    assert result.exit_code == 0
    assert mock_prune.call_args.kwargs["yes"] is True


def test_prune_and_rebase_share_yes_option(xdg):
    """-y/--yes is the same option on both commands."""
    prune_help = runner.invoke(app, ["prune", "--help"]).output
    rebase_help = runner.invoke(app, ["rebase", "--help"]).output
    assert "Skip the confirmation prompt" in _unwrap(rebase_help)
    assert "Skip the confirmation prompt" in _unwrap(prune_help)


def test_prune_dry_run_help_describes_cleanup(xdg):
    """--dry-run on prune describes its own survey, not git commands alone."""
    prune_help = _unwrap(runner.invoke(app, ["prune", "--help"]).output)
    assert "cleanup" in prune_help.lower() or "dead index" in prune_help.lower()


def test_prune_help_mentions_dead_index_entries(xdg):
    """The one-liner described a command that no longer exists."""
    help_text = _unwrap(runner.invoke(app, ["prune", "--help"]).output)
    assert "dead index entries" in help_text


def test_prune_does_not_bootstrap_the_global_config(xdg):
    """cmd_prune reads no Config, so prune must not create one to hand it."""
    assert not paths.config_file().exists()

    with patch("ow.__main__.cmd_prune", autospec=True):
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    assert not paths.config_file().exists()


def test_prune_still_gates_on_the_legacy_layout(xdg, tmp_path):
    """Dropping _load_config() must not drop the migration pointer with it."""
    (tmp_path / "ow.toml").write_text("")

    with patch("ow.__main__.cmd_prune", autospec=True) as mock_prune:
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 1
    assert "ow.toml" in result.output
    mock_prune.assert_not_called()


def test_ls(xdg):
    """ow ls wiring: __main__.ls() calls cmd_ls() with archived=False, and does
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
    mock_ls.assert_called_once_with(archived=False)
    mock_load_config.assert_not_called()


def test_ls_archived(xdg):
    """--archived reaches cmd_ls, and still needs no Config."""
    with patch("ow.__main__.cmd_ls", autospec=True) as mock_ls, \
            patch("ow.__main__._load_config", autospec=True) as mock_load_config:
        result = runner.invoke(app, ["ls", "--archived"])

    assert result.exit_code == 0
    mock_ls.assert_called_once_with(archived=True)
    mock_load_config.assert_not_called()


def test_creates_config_if_missing(xdg):
    """If the global config doesn't exist yet, it is bootstrapped with default content."""
    assert not paths.config_file().exists()

    with patch("ow.__main__.cmd_status", autospec=True):
        result = runner.invoke(app, ["status"])

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
    """Call the autocompletion callback Click's ShellComplete would invoke.

    Click 8.5 changed _resolve_context so it no longer descends into
    subcommands during completion, which makes ShellComplete return
    command names instead of parameter completions. We test our
    completer callbacks, not Click's shell integration, so we resolve
    the subcommand and call the first parameter with `autocompletion`.
    """
    from typer.main import get_command

    cmd = get_command(app)
    ctx = cmd.make_context("ow", args, resilient_parsing=True)

    # TyperGroup in Typer >= 0.16 inherits from typer's own Command shim,
    # not click.Group — check for the method instead.
    if not args or not hasattr(ctx.command, "get_command"):
        return []
    name = args[0]
    sub_cmd = ctx.command.get_command(ctx, name)
    if sub_cmd is None:
        return []
    sub_ctx = sub_cmd.make_context(
        name, args[1:], parent=ctx, resilient_parsing=True,
    )
    # Find which param the completion is for: the last complete arg is
    # an option flag (e.g. "-r"), and we want that option's completions.
    # If the last arg is not a flag, it's an argument completion.
    last_arg = args[-1] if args else ""
    if last_arg.startswith("-"):
        for param in sub_ctx.command.get_params(sub_ctx):
            opts = getattr(param, "opts", [])
            if last_arg in opts:
                items = param.shell_complete(sub_ctx, incomplete)
                return [item.value for item in items]

    for param in sub_ctx.command.get_params(sub_ctx):
        # Click 8.5: shell_complete(ctx, incomplete) → list[CompletionItem].
        # Every param has it as a base method, so only stop when it
        # actually returns something.
        items = param.shell_complete(sub_ctx, incomplete)
        if items:
            return [item.value for item in items]
    return []


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




def test_complete_workspace_name_does_not_mutate_index(xdg, tmp_path):
    """Completion runs on every keystroke and must not prune or rewrite."""
    from ow.utils import index

    live = tmp_path / "workspaces" / "live"
    (live / ".ow").mkdir(parents=True)
    (live / ".ow" / "config.toml").write_text("")
    index.remember(live)

    dead = tmp_path / "workspaces" / "dead"
    (dead / ".ow").mkdir(parents=True)
    (dead / ".ow" / "config.toml").write_text("")
    index.remember(dead)
    shutil.rmtree(dead)

    before = paths.index_file().read_text()
    _complete(["status"], "")
    assert paths.index_file().read_text() == before


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
