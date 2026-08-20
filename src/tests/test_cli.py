from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ow.__main__ import app
from ow.utils import paths
from ow.utils.config import BranchSpec

runner = CliRunner()


def test_no_args_shows_help():
    """ow without args shows help (no_args_is_help=True)."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Odoo workspace manager" in result.output


@pytest.mark.parametrize("flag", ["--version", "-V", "-v"])
def test_version_flag(flag):
    """-v was the argparse spelling; keep it working alongside -V."""
    result = runner.invoke(app, [flag])
    assert result.exit_code == 0
    assert result.output.startswith("ow ")


def test_create_with_args(xdg):
    """ow create -n myws -r community:master..x -t common calls cmd_create with correct args."""
    with patch("ow.__main__.cmd_create") as mock_create:
        result = runner.invoke(app, [
            "create",
            "-n", "myws",
            "-r", "community:master..x",
            "-t", "common",
        ])

    assert result.exit_code == 0
    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args
    assert call_kwargs.kwargs["name"] == "myws"
    assert call_kwargs.kwargs["templates"] == ["common"]
    assert "community" in call_kwargs.kwargs["repos"]
    assert call_kwargs.kwargs["repos"]["community"] == BranchSpec("origin/master", "x")


def test_create_rejects_repo_without_spec(xdg):
    """-r ALIAS with no ':' must fail loudly, not silently drop the repo."""
    with patch("ow.__main__.cmd_create") as mock_create:
        result = runner.invoke(app, ["create", "-n", "myws", "-r", "community"])

    assert result.exit_code != 0
    assert "ALIAS:SPEC" in result.output
    assert "community:master..x" in result.output
    mock_create.assert_not_called()


def test_create_rejects_repo_with_empty_alias(xdg):
    """-r :spec has no alias to attach the spec to."""
    with patch("ow.__main__.cmd_create") as mock_create:
        result = runner.invoke(app, ["create", "-n", "myws", "-r", ":master..x"])

    assert result.exit_code != 0
    assert "ALIAS:SPEC" in result.output
    mock_create.assert_not_called()


def test_create_accepts_several_repos(xdg):
    """-r is repeatable."""
    with patch("ow.__main__.cmd_create") as mock_create:
        result = runner.invoke(app, [
            "create", "-n", "myws",
            "-r", "community:master..x",
            "-r", "enterprise:master..x",
        ])

    assert result.exit_code == 0
    repos = mock_create.call_args.kwargs["repos"]
    assert sorted(repos) == ["community", "enterprise"]


def test_update(xdg):
    """ow update calls cmd_update."""
    with patch("ow.__main__.cmd_update") as mock_update:
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    mock_update.assert_called_once()


def test_update_with_workspace(xdg):
    """ow update myws calls cmd_update with workspace="myws"."""
    with patch("ow.__main__.cmd_update") as mock_update:
        result = runner.invoke(app, ["update", "myws"])

    assert result.exit_code == 0
    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs["workspace"] == "myws"


def test_status_with_workspace(xdg):
    """ow status myws calls cmd_status with workspace="myws"."""
    with patch("ow.__main__.cmd_status") as mock_status:
        result = runner.invoke(app, ["status", "myws"])

    assert result.exit_code == 0
    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] == "myws"


def test_status_without_workspace(xdg):
    """ow status calls cmd_status with workspace=None."""
    with patch("ow.__main__.cmd_status") as mock_status:
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] is None


def test_rebase_with_workspace(xdg):
    """ow rebase myws calls cmd_rebase with workspace="myws"."""
    with patch("ow.__main__.cmd_rebase") as mock_rebase:
        result = runner.invoke(app, ["rebase", "myws"])

    assert result.exit_code == 0
    mock_rebase.assert_called_once()
    assert mock_rebase.call_args.kwargs["workspace"] == "myws"


def test_prune(xdg):
    """ow prune calls cmd_prune."""
    with patch("ow.__main__.cmd_prune") as mock_prune:
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    mock_prune.assert_called_once()


def test_creates_config_if_missing(xdg):
    """If the global config doesn't exist yet, it is bootstrapped with default content."""
    assert not paths.config_file().exists()

    with patch("ow.__main__.cmd_prune"):
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    assert paths.config_file().exists()
    content = paths.config_file().read_text()
    assert "community" in content
    assert "origin.url" in content


def test_exits_nonzero_if_config_load_fails(xdg):
    """A config that cannot be loaded surfaces as a CLI failure, not a silent crash."""
    with patch("ow.__main__.load_global_config", side_effect=OSError("boom")):
        result = runner.invoke(app, ["status"])

    assert result.exit_code != 0


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
    names = _complete(["create", "-t"], "")
    assert "common" in names
    assert "vscode" in names
    assert "zed" in names


def test_complete_gen_templates_with_prefix(xdg):
    """Template completion filters by prefix."""
    _make_templates("common", "vscode")
    assert _complete(["create", "-t"], "v") == ["vscode"]


def test_complete_gen_templates_none_taken(xdg):
    """Template completion still offers the packaged templates when nothing local exists."""
    names = _complete(["create", "-t"], "")
    assert "common" in names
    assert "vscode" in names


def test_complete_gen_repos(xdg):
    """Repo completion returns unused aliases."""
    _write_remotes("community", "enterprise")
    names = _complete(["create", "-r"], "")
    assert "community" in names
    assert "enterprise" in names


def test_complete_gen_repos_excludes_used(xdg):
    """Repo completion excludes aliases already given on the command line."""
    _write_remotes("community", "enterprise")
    names = _complete(["create", "-r", "community:master", "-r"], "")
    assert "community" not in names
    assert "enterprise" in names


def test_complete_gen_repos_with_prefix(xdg):
    """Repo completion filters by prefix."""
    _write_remotes("community", "enterprise")
    assert _complete(["create", "-r"], "e") == ["enterprise"]


def test_complete_gen_repos_default_bootstrap(xdg):
    """First run with no config file yet: bootstrap seeds the default community remote."""
    assert _complete(["create", "-r"], "") == ["community"]


def test_complete_workspace_name_disabled(xdg):
    """Workspace name completion is disabled until task 5 adds the discovery index."""
    assert _complete(["status"], "") == []


class TestRebaseFlags:
    def test_flags_reach_cmd_rebase(self):
        from typer.testing import CliRunner
        from ow.__main__ import app
        with patch("ow.__main__.cmd_rebase") as mock, patch("ow.__main__._load_config"):
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
        with patch("ow.__main__.cmd_rebase") as mock, patch("ow.__main__._load_config"):
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
