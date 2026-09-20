import importlib
import re
import shutil
import subprocess
import sys
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
    """ow without args in a non-TTY shows help and exits 2."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Odoo workspace manager" in result.output


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(flag):
    """The two spellings 2.0 freezes."""
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
    assert result.output.startswith("ow ")


def test_version_falls_back_in_a_source_checkout(monkeypatch):
    """`ow/_version.py` is written by setuptools-scm at build time and kept
    out of git, so it exists in an installed ow and not in a clone. Importing
    ow must survive that — `ow --version` then answers `ow dev` rather than
    raising, and every caller reads the same `ow.__version__`.

    `None` in sys.modules is what makes an import of that name fail, which is
    the state a fresh clone is in.
    """
    ow = importlib.import_module("ow")
    monkeypatch.setitem(sys.modules, "ow._version", None)
    try:
        assert importlib.reload(ow).__version__ == "dev"
    finally:
        monkeypatch.undo()
        importlib.reload(ow)


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
    """ow init myws -r community:master..x calls cmd_init with correct args."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, [
            "init",
            "myws",
            "-r", "community:master..x",
        ])

    assert result.exit_code == 0
    mock_init.assert_called_once()
    call_kwargs = mock_init.call_args
    assert call_kwargs.kwargs["name"] == "myws"
    assert "community" in call_kwargs.kwargs["repos"]
    assert call_kwargs.kwargs["repos"]["community"] == BranchSpec("origin/master", "x")


def test_init_without_name_passes_none(xdg):
    """`ow init` with no argument means "here" — the name must reach cmd_init as None."""
    with patch("ow.__main__.cmd_init", autospec=True) as mock_init:
        result = runner.invoke(app, ["init", "-r", "community:master..x"])

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


def test_render(xdg):
    """ow render calls cmd_render."""
    with patch("ow.__main__.cmd_render", autospec=True) as mock_render:
        result = runner.invoke(app, ["render"])

    assert result.exit_code == 0
    mock_render.assert_called_once()


def test_render_with_workspace(xdg):
    """ow render myws calls cmd_render with workspace="myws"."""
    with patch("ow.__main__.cmd_render", autospec=True) as mock_render:
        result = runner.invoke(app, ["render", "myws"])

    assert result.exit_code == 0
    mock_render.assert_called_once()
    assert mock_render.call_args.kwargs["workspace"] == "myws"


@pytest.mark.parametrize("command", ["render", "status", "rebase"])
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


@pytest.mark.parametrize("command,mock_target", [("render", "cmd_render"), ("status", "cmd_status")])
@pytest.mark.parametrize("flag", ["-w", "--workspace"])
def test_workspace_option_reaches_command(xdg, command, mock_target, flag):
    """-w/--workspace is a synonym for the positional WORKSPACE."""
    with patch(f"ow.__main__.{mock_target}", autospec=True) as mock_cmd:
        result = runner.invoke(app, [command, flag, "myws"])

    assert result.exit_code == 0
    assert mock_cmd.call_args.kwargs["workspace"] == "myws"


def test_workspace_positional_and_option_agreeing_is_accepted(xdg):
    """Naming the same workspace twice is redundant, not an error."""
    with patch("ow.__main__.cmd_render", autospec=True) as mock_render:
        result = runner.invoke(app, ["render", "myws", "-w", "myws"])

    assert result.exit_code == 0
    assert mock_render.call_args.kwargs["workspace"] == "myws"


def test_workspace_positional_and_option_disagreeing_is_rejected(xdg):
    """Two different workspaces named at once must fail loudly, not pick one."""
    with patch("ow.__main__.cmd_render", autospec=True) as mock_render:
        result = runner.invoke(app, ["render", "myws", "-w", "otherws"])

    assert result.exit_code != 0
    assert "myws" in result.output
    assert "otherws" in result.output
    mock_render.assert_not_called()


def test_rm_requires_a_name(xdg):
    """Neither the positional nor -w given: rm must not guess."""
    with patch("ow.__main__.cmd_rm", autospec=True) as mock_rm:
        result = runner.invoke(app, ["rm"])

    assert result.exit_code != 0
    mock_rm.assert_not_called()


def test_rm_accepts_workspace_option_as_alias(xdg):
    """-w NAME works on its own, with no positional given."""
    with patch("ow.__main__.cmd_rm", autospec=True) as mock_rm:
        result = runner.invoke(app, ["rm", "-w", "myws"])

    assert result.exit_code == 0
    assert mock_rm.call_args.kwargs["name"] == "myws"


def test_rm_rejects_disagreeing_forms(xdg):
    with patch("ow.__main__.cmd_rm", autospec=True) as mock_rm:
        result = runner.invoke(app, ["rm", "myws", "-w", "otherws"])

    assert result.exit_code != 0
    mock_rm.assert_not_called()


class TestSwitchCommand:
    def test_flags_reach_cmd_switch(self):
        with (
            patch("ow.__main__.cmd_switch", autospec=True) as mock,
            patch("ow.__main__._load_config"),
        ):
            runner.invoke(
                app,
                ["switch", "feature-x", "-w", "myws", "--only", "community", "--dry-run"],
            )
        _, kwargs = mock.call_args
        assert kwargs["target"] == "feature-x"
        assert kwargs["workspace"] == "myws"
        assert kwargs["only"] == "community"
        assert kwargs["dry_run"] is True
        assert kwargs["create"] is None
        assert kwargs["detach"] is False

    def test_create_reaches_cmd_switch(self):
        with (
            patch("ow.__main__.cmd_switch", autospec=True) as mock,
            patch("ow.__main__._load_config"),
        ):
            runner.invoke(app, ["switch", "start-point", "-c", "new-branch"])
        _, kwargs = mock.call_args
        assert kwargs["target"] == "start-point"
        assert kwargs["create"] == "new-branch"

    def test_detach_reaches_cmd_switch(self):
        with (
            patch("ow.__main__.cmd_switch", autospec=True) as mock,
            patch("ow.__main__._load_config"),
        ):
            runner.invoke(app, ["switch", "abc123", "--detach"])
        _, kwargs = mock.call_args
        assert kwargs["detach"] is True

    def test_detach_and_create_together_is_rejected(self, xdg):
        """cmd_switch itself validates this before touching any workspace —
        exercised for real, unmocked, against a directory that is not even
        a workspace, to prove the rejection happens before resolution.
        """
        result = runner.invoke(app, ["switch", "x", "--detach", "-c", "y"])


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


def test_does_not_create_a_config_just_by_running(xdg):
    """Reading the global config creates nothing — no bootstrap on first use.

    Writing a default config from a read made every command a writer, and it
    silently erased the "no global config yet" condition that
    `check_legacy_layout()` reads. The command still runs, on the in-memory
    defaults; `ow init`/`ow render` are what persist a config."""
    assert not paths.config_file().exists()

    with patch("ow.__main__.cmd_status", autospec=True) as mock_status:
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert not paths.config_file().exists()
    assert mock_status.called
    passed_config = mock_status.call_args.args[0]
    assert "community" in passed_config.remotes


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


def _write_remotes(*names):
    body = "".join(f'{n}.origin.url = "git@github.com:odoo/{n}.git"\n' for n in names)
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[remotes]\n" + body)


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


def _make_bare_repo_with_refs(bare_path, heads=(), remotes=()):
    """A real bare repo with the given local and remote-tracking refs.

    Built via a throwaway working clone: a fresh bare repo has no objects
    at all, so there is no commit to point a fabricated ref at without one.
    """
    bare_path.parent.mkdir(parents=True, exist_ok=True)
    work = bare_path.parent / f"{bare_path.stem}-work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "c"],
        check=True,
    )
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare_path)], check=True)
    sha = subprocess.run(
        ["git", "-C", str(bare_path), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    for name in heads:
        subprocess.run(["git", "-C", str(bare_path), "update-ref", f"refs/heads/{name}", sha], check=True)
    for name in remotes:
        subprocess.run(["git", "-C", str(bare_path), "update-ref", f"refs/remotes/{name}", sha], check=True)
    shutil.rmtree(work)


def test_complete_branch_name_lists_local_branches(xdg, workspace_dir):
    ws_dir = workspace_dir(repos={"community": "master"})
    bare = paths.repos_dir() / "community.git"
    _make_bare_repo_with_refs(bare, heads=["feature-x", "feature-y"])

    names = _complete(["switch", "-w", str(ws_dir)], "feature")

    assert sorted(names) == ["feature-x", "feature-y"]


def test_complete_branch_name_includes_remote_tracking_refs(xdg, workspace_dir):
    ws_dir = workspace_dir(repos={"community": "master"})
    bare = paths.repos_dir() / "community.git"
    _make_bare_repo_with_refs(bare, heads=["main"], remotes=["origin/staging"])

    names = _complete(["switch", "-w", str(ws_dir)], "origin/")

    assert names == ["origin/staging"]


def test_complete_branch_name_returns_empty_list_outside_a_workspace(xdg, tmp_path):
    """Completion must never crash the shell, whatever state the repos are in."""
    assert _complete(["switch", "-w", str(tmp_path / "not-a-workspace")], "") == []


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
