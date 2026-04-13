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


def test_create_with_args(tmp_path):
    """ow create -n myws -r community master..x -t common calls cmd_create with correct args."""
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


def test_complete_gen_templates(tmp_path):
    """Template completion returns correct template names."""
    templates_dir = tmp_path / "templates"
    (templates_dir / "common").mkdir(parents=True)
    (templates_dir / "vscode").mkdir(parents=True)
    (templates_dir / "zed").mkdir(parents=True)

    ctx = MagicMock()
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_gen_templates(ctx, "")

    names = [c.value for c in result]
    assert "common" in names
    assert "vscode" in names
    assert "zed" in names


def test_complete_gen_templates_with_prefix(tmp_path):
    """Template completion filters by prefix."""
    templates_dir = tmp_path / "templates"
    (templates_dir / "common").mkdir(parents=True)
    (templates_dir / "vscode").mkdir(parents=True)

    ctx = MagicMock()
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_gen_templates(ctx, "v")

    names = [c.value for c in result]
    assert names == ["vscode"]


def test_complete_gen_templates_no_root():
    """Template completion returns empty list if root not found."""
    ctx = MagicMock()
    with patch("ow.__main__._find_root", side_effect=FileNotFoundError):
        result = complete_gen_templates(ctx, "")

    assert result == []


def test_complete_gen_repos(tmp_path):
    """Repo completion returns unused aliases."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    ctx = MagicMock(args=[])
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_gen_repos(ctx, "")

    names = [c.value for c in result]
    assert "community" in names
    assert "enterprise" in names


def test_complete_gen_repos_excludes_used(tmp_path):
    """Repo completion excludes already-provided aliases."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    ctx = MagicMock(args=["-r", "community:master"])
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_gen_repos(ctx, "")

    names = [c.value for c in result]
    assert "community" not in names
    assert "enterprise" in names


def test_complete_gen_repos_with_prefix(tmp_path):
    """Repo completion filters by prefix."""
    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    ctx = MagicMock(args=[])
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_gen_repos(ctx, "e")

    names = [c.value for c in result]
    assert names == ["enterprise"]


def test_complete_gen_repos_no_root():
    """Repo completion returns empty list if root not found."""
    ctx = MagicMock(args=[])
    with patch("ow.__main__._find_root", side_effect=FileNotFoundError):
        result = complete_gen_repos(ctx, "")

    assert result == []


def test_complete_workspace_name(tmp_path):
    """Workspace completion returns existing workspace names."""
    ws_dir = tmp_path / "workspaces"
    (ws_dir / "alpha").mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").touch()

    (ws_dir / "beta").mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").touch()

    (ws_dir / "gamma").mkdir(parents=True)

    ctx = MagicMock()
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_workspace_name(ctx, "")

    names = [c.value for c in result]
    assert "alpha" in names
    assert "beta" in names
    assert "gamma" not in names


def test_complete_workspace_name_with_prefix(tmp_path):
    """Workspace completion filters by prefix."""
    ws_dir = tmp_path / "workspaces"
    (ws_dir / "alpha").mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").touch()

    (ws_dir / "beta").mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").touch()

    ctx = MagicMock()
    with patch("ow.__main__._find_root", return_value=tmp_path):
        result = complete_workspace_name(ctx, "a")

    names = [c.value for c in result]
    assert names == ["alpha"]


def test_complete_workspace_name_no_root():
    """Workspace completion returns empty list if root not found."""
    ctx = MagicMock()
    with patch("ow.__main__._find_root", side_effect=FileNotFoundError):
        result = complete_workspace_name(ctx, "")

    assert result == []
