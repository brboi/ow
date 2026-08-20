from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ow.__main__ import app, complete_gen_repos, complete_gen_templates, complete_workspace_name
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


def test_create_with_args(tmp_path):
    """ow create -n myws -r community:master..x -t common calls cmd_create with correct args."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_create") as mock_create,
    ):
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


def test_create_rejects_repo_without_spec(tmp_path):
    """-r ALIAS with no ':' must fail loudly, not silently drop the repo."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_create") as mock_create,
    ):
        result = runner.invoke(app, ["create", "-n", "myws", "-r", "community"])

    assert result.exit_code != 0
    assert "ALIAS:SPEC" in result.output
    assert "community:master..x" in result.output
    mock_create.assert_not_called()


def test_create_rejects_repo_with_empty_alias(tmp_path):
    """-r :spec has no alias to attach the spec to."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_create") as mock_create,
    ):
        result = runner.invoke(app, ["create", "-n", "myws", "-r", ":master..x"])

    assert result.exit_code != 0
    assert "ALIAS:SPEC" in result.output
    mock_create.assert_not_called()


def test_create_accepts_several_repos(tmp_path):
    """-r is repeatable."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_create") as mock_create,
    ):
        result = runner.invoke(app, [
            "create", "-n", "myws",
            "-r", "community:master..x",
            "-r", "enterprise:master..x",
        ])

    assert result.exit_code == 0
    repos = mock_create.call_args.kwargs["repos"]
    assert sorted(repos) == ["community", "enterprise"]


def test_update(tmp_path):
    """ow update calls cmd_update."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_update") as mock_update,
    ):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    mock_update.assert_called_once()


def test_update_with_workspace(tmp_path):
    """ow update myws calls cmd_update with workspace="myws"."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_update") as mock_update,
    ):
        result = runner.invoke(app, ["update", "myws"])

    assert result.exit_code == 0
    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs["workspace"] == "myws"


def test_status_with_workspace(tmp_path):
    """ow status myws calls cmd_status with workspace="myws"."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_status") as mock_status,
    ):
        result = runner.invoke(app, ["status", "myws"])

    assert result.exit_code == 0
    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] == "myws"


def test_status_without_workspace(tmp_path):
    """ow status calls cmd_status with workspace=None."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_status") as mock_status,
    ):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] is None


def test_rebase_with_workspace(tmp_path):
    """ow rebase myws calls cmd_rebase with workspace="myws"."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_rebase") as mock_rebase,
    ):
        result = runner.invoke(app, ["rebase", "myws"])

    assert result.exit_code == 0
    mock_rebase.assert_called_once()
    assert mock_rebase.call_args.kwargs["workspace"] == "myws"


def test_prune(tmp_path):
    """ow prune calls cmd_prune."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_prune") as mock_prune,
    ):
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    mock_prune.assert_called_once()


def test_creates_ow_toml_if_missing(tmp_path):
    """If ow.toml doesn't exist, it is created with minimal content."""
    with (
        patch("ow.__main__._find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_prune"),
    ):
        result = runner.invoke(app, ["prune"])

    assert result.exit_code == 0
    toml_path = tmp_path / "ow.toml"
    assert toml_path.exists()
    content = toml_path.read_text()
    assert "community.origin.url" in content


def test_exits_if_root_not_found():
    """If _find_root fails, displays error and exits with code 1."""
    with patch("ow.__main__._find_root", side_effect=FileNotFoundError("ow.toml not found")):
        result = runner.invoke(app, ["status"])

    assert result.exit_code == 1
    assert "ow.toml not found" in result.output


# ---------------------------------------------------------------------------
# Tab completion
#
# These drive Click's real ShellComplete path rather than calling the callbacks
# with a hand-built context: the callback signature and the return type are
# part of the contract, and a fake context hides breakage in both.
# ---------------------------------------------------------------------------


def _complete(args, incomplete, root=None, missing_root=False):
    from click.shell_completion import ShellComplete
    from typer.main import get_command

    comp = ShellComplete(get_command(app), {}, "ow", "_OW_COMPLETE")
    kwargs = {"side_effect": FileNotFoundError} if missing_root else {"return_value": root}
    with patch("ow.__main__._find_root", **kwargs):
        return [item.value for item in comp.get_completions(args, incomplete)]


def _make_templates(root, *names):
    for name in names:
        (root / "templates" / name).mkdir(parents=True)


def _make_remotes(root, *names):
    body = "".join(f'{n}.origin.url = "git@github.com:odoo/{n}.git"\n' for n in names)
    (root / "ow.toml").write_text("[remotes]\n" + body)


def _make_workspaces(root, *names, invalid=()):
    for name in names:
        (root / "workspaces" / name / ".ow").mkdir(parents=True)
        (root / "workspaces" / name / ".ow" / "config.toml").touch()
    for name in invalid:
        (root / "workspaces" / name).mkdir(parents=True)


def test_complete_gen_templates(tmp_path):
    """Template completion returns correct template names."""
    _make_templates(tmp_path, "common", "vscode", "zed")
    names = _complete(["create", "-t"], "", tmp_path)
    assert "common" in names
    assert "vscode" in names
    assert "zed" in names


def test_complete_gen_templates_with_prefix(tmp_path):
    """Template completion filters by prefix."""
    _make_templates(tmp_path, "common", "vscode")
    assert _complete(["create", "-t"], "v", tmp_path) == ["vscode"]


def test_complete_gen_templates_no_root():
    """Template completion returns empty list if root not found."""
    assert _complete(["create", "-t"], "", missing_root=True) == []


def test_complete_gen_repos(tmp_path):
    """Repo completion returns unused aliases."""
    _make_remotes(tmp_path, "community", "enterprise")
    names = _complete(["create", "-r"], "", tmp_path)
    assert "community" in names
    assert "enterprise" in names


def test_complete_gen_repos_excludes_used(tmp_path):
    """Repo completion excludes aliases already given on the command line."""
    _make_remotes(tmp_path, "community", "enterprise")
    names = _complete(["create", "-r", "community:master", "-r"], "", tmp_path)
    assert "community" not in names
    assert "enterprise" in names


def test_complete_gen_repos_with_prefix(tmp_path):
    """Repo completion filters by prefix."""
    _make_remotes(tmp_path, "community", "enterprise")
    assert _complete(["create", "-r"], "e", tmp_path) == ["enterprise"]


def test_complete_gen_repos_no_root():
    """Repo completion returns empty list if root not found."""
    assert _complete(["create", "-r"], "", missing_root=True) == []


def test_complete_workspace_name(tmp_path):
    """Workspace completion returns existing workspace names."""
    _make_workspaces(tmp_path, "alpha", "beta", invalid=("gamma",))
    names = _complete(["status"], "", tmp_path)
    assert "alpha" in names
    assert "beta" in names
    assert "gamma" not in names


def test_complete_workspace_name_with_prefix(tmp_path):
    """Workspace completion filters by prefix."""
    _make_workspaces(tmp_path, "alpha", "beta")
    assert _complete(["status"], "a", tmp_path) == ["alpha"]


def test_complete_workspace_name_no_root():
    """Workspace completion returns empty list if root not found."""
    assert _complete(["status"], "", missing_root=True) == []


def test_complete_workspace_name_survives_unreadable_dir(tmp_path):
    """Completion must never crash the shell, even on an unreadable workspaces/."""
    (tmp_path / "workspaces").mkdir()
    with patch.object(Path, "iterdir", side_effect=PermissionError):
        assert _complete(["status"], "", tmp_path) == []


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
