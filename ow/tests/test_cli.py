import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.config import BranchSpec


# ---------------------------------------------------------------------------
# test_main_no_args_exits
# ---------------------------------------------------------------------------

def test_main_no_args_exits(capsys):
    """ow without command exits with argparse error (required=True)."""
    from ow.__main__ import main

    with patch.object(sys, "argv", ["ow"]):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "required" in captured.err.lower()


# ---------------------------------------------------------------------------
# test_main_create_with_args
# ---------------------------------------------------------------------------

def test_main_create_with_args(tmp_path):
    """ow create -n myws -r community master..x -t common calls cmd_create with correct args."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_create") as mock_create,
        patch.object(sys, "argv", [
            "ow", "create",
            "-n", "myws",
            "-r", "community", "master..x",
            "-t", "common",
        ]),
    ):
        main()

    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args
    assert call_kwargs.kwargs["name"] == "myws"
    assert call_kwargs.kwargs["templates"] == ["common"]
    assert "community" in call_kwargs.kwargs["repos"]
    assert call_kwargs.kwargs["repos"]["community"] == BranchSpec("origin/master", "x")


# ---------------------------------------------------------------------------
# test_main_update
# ---------------------------------------------------------------------------

def test_main_update(tmp_path):
    """ow update calls cmd_update."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_update") as mock_update,
        patch.object(sys, "argv", ["ow", "update"]),
    ):
        main()

    mock_update.assert_called_once()


def test_main_update_with_workspace(tmp_path):
    """ow update myws calls cmd_update with workspace="myws"."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_update") as mock_update,
        patch.object(sys, "argv", ["ow", "update", "myws"]),
    ):
        main()

    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs["workspace"] == "myws"


# ---------------------------------------------------------------------------
# test_main_status_with_workspace
# ---------------------------------------------------------------------------

def test_main_status_with_workspace(tmp_path):
    """ow status myws calls cmd_status with workspace="myws"."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_status") as mock_status,
        patch.object(sys, "argv", ["ow", "status", "myws"]),
    ):
        main()

    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] == "myws"


# ---------------------------------------------------------------------------
# test_main_status_without_workspace
# ---------------------------------------------------------------------------

def test_main_status_without_workspace(tmp_path):
    """ow status calls cmd_status with workspace=None."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_status") as mock_status,
        patch.object(sys, "argv", ["ow", "status"]),
    ):
        main()

    mock_status.assert_called_once()
    assert mock_status.call_args.kwargs["workspace"] is None


# ---------------------------------------------------------------------------
# test_main_rebase_with_workspace
# ---------------------------------------------------------------------------

def test_main_rebase_with_workspace(tmp_path):
    """ow rebase myws calls cmd_rebase with workspace="myws"."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_rebase") as mock_rebase,
        patch.object(sys, "argv", ["ow", "rebase", "myws"]),
    ):
        main()

    mock_rebase.assert_called_once()
    assert mock_rebase.call_args.kwargs["workspace"] == "myws"


# ---------------------------------------------------------------------------
# test_main_prune
# ---------------------------------------------------------------------------

def test_main_prune(tmp_path):
    """ow prune calls cmd_prune."""
    from ow.__main__ import main

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
    )

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_prune") as mock_prune,
        patch.object(sys, "argv", ["ow", "prune"]),
    ):
        main()

    mock_prune.assert_called_once()


# ---------------------------------------------------------------------------
# test_main_creates_ow_toml_if_missing
# ---------------------------------------------------------------------------

def test_main_creates_ow_toml_if_missing(tmp_path, capsys):
    """If ow.toml doesn't exist, it is created with minimal content."""
    from ow.__main__ import main

    with (
        patch("ow.__main__.find_root", return_value=tmp_path),
        patch("ow.__main__.cmd_prune"),
        patch.object(sys, "argv", ["ow", "prune"]),
    ):
        main()

    toml_path = tmp_path / "ow.toml"
    assert toml_path.exists()
    content = toml_path.read_text()
    assert "community.origin.url" in content

    captured = capsys.readouterr()
    assert "Created ow.toml" in captured.out


# ---------------------------------------------------------------------------
# test_main_exits_if_root_not_found
# ---------------------------------------------------------------------------

def test_main_exits_if_root_not_found(capsys):
    """If find_root fails, displays error and exits with code 1."""
    from ow.__main__ import main

    with (
        patch("ow.__main__.find_root", side_effect=FileNotFoundError("ow.toml not found")),
        patch.object(sys, "argv", ["ow", "status"]),
    ):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "ow.toml not found" in captured.err


# ---------------------------------------------------------------------------
# test_complete_gen_templates
# ---------------------------------------------------------------------------

def test_complete_gen_templates(tmp_path):
    """Template completion returns correct template names."""
    from ow.__main__ import _complete_gen_templates

    templates_dir = tmp_path / "templates"
    (templates_dir / "common").mkdir(parents=True)
    (templates_dir / "vscode").mkdir(parents=True)
    (templates_dir / "zed").mkdir(parents=True)

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_gen_templates("", MagicMock())

    assert "common" in result
    assert "vscode" in result
    assert "zed" in result
    assert "bwrap" in result


def test_complete_gen_templates_with_prefix(tmp_path):
    """Template completion filters by prefix."""
    from ow.__main__ import _complete_gen_templates

    templates_dir = tmp_path / "templates"
    (templates_dir / "common").mkdir(parents=True)
    (templates_dir / "vscode").mkdir(parents=True)

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_gen_templates("v", MagicMock())

    assert result == ["vscode"]


def test_complete_gen_templates_no_root(capsys):
    """Template completion returns empty list if root not found."""
    from ow.__main__ import _complete_gen_templates

    with patch("ow.__main__.find_root", side_effect=FileNotFoundError):
        result = _complete_gen_templates("", MagicMock())

    assert result == []


# ---------------------------------------------------------------------------
# test_complete_gen_repos
# ---------------------------------------------------------------------------

def test_complete_gen_repos(tmp_path):
    """Repo completion returns unused aliases."""
    from ow.__main__ import _complete_gen_repos

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    parsed_args = MagicMock(repo=None)

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_gen_repos("", parsed_args)

    assert "community" in result
    assert "enterprise" in result


def test_complete_gen_repos_excludes_used(tmp_path):
    """Repo completion excludes already-provided aliases."""
    from ow.__main__ import _complete_gen_repos

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    parsed_args = MagicMock(repo=[["community", "master"]])

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_gen_repos("", parsed_args)

    assert "community" not in result
    assert "enterprise" in result


def test_complete_gen_repos_with_prefix(tmp_path):
    """Repo completion filters by prefix."""
    from ow.__main__ import _complete_gen_repos

    (tmp_path / "ow.toml").write_text(
        '[remotes]\ncommunity.origin.url = "git@github.com:odoo/odoo.git"\n'
        'enterprise.origin.url = "git@github.com:odoo/enterprise.git"\n'
    )

    parsed_args = MagicMock(repo=None)

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_gen_repos("e", parsed_args)

    assert result == ["enterprise"]


def test_complete_gen_repos_no_root():
    """Repo completion returns empty list if root not found."""
    from ow.__main__ import _complete_gen_repos

    parsed_args = MagicMock(repo=None)

    with patch("ow.__main__.find_root", side_effect=FileNotFoundError):
        result = _complete_gen_repos("", parsed_args)

    assert result == []


# ---------------------------------------------------------------------------
# test_complete_workspace_name
# ---------------------------------------------------------------------------

def test_complete_workspace_name(tmp_path):
    """Workspace completion returns existing workspace names."""
    from ow.__main__ import _complete_workspace_name

    ws_dir = tmp_path / "workspaces"
    (ws_dir / "alpha").mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").touch()

    (ws_dir / "beta").mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").touch()

    (ws_dir / "gamma").mkdir(parents=True)  # no .ow/config

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_workspace_name("", MagicMock())

    assert "alpha" in result
    assert "beta" in result
    assert "gamma" not in result


def test_complete_workspace_name_with_prefix(tmp_path):
    """Workspace completion filters by prefix."""
    from ow.__main__ import _complete_workspace_name

    ws_dir = tmp_path / "workspaces"
    (ws_dir / "alpha").mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "alpha" / ".ow" / "config").touch()

    (ws_dir / "beta").mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").parent.mkdir(parents=True)
    (ws_dir / "beta" / ".ow" / "config").touch()

    with patch("ow.__main__.find_root", return_value=tmp_path):
        result = _complete_workspace_name("a", MagicMock())

    assert result == ["alpha"]


def test_complete_workspace_name_no_root():
    """Workspace completion returns empty list if root not found."""
    from ow.__main__ import _complete_workspace_name

    with patch("ow.__main__.find_root", side_effect=FileNotFoundError):
        result = _complete_workspace_name("", MagicMock())

    assert result == []
