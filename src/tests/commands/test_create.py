from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_create
from ow.commands.create import _check_duplicate_branches, _cleanup_failed_workspace
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config


def _make_config(
    root_dir=None,
    vars=None,
    remotes=None,
) -> Config:
    return Config(
        vars=vars
        if vars is not None
        else {"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes=remotes or {},
        root_dir=root_dir or Path("/root"),
    )


# ---------------------------------------------------------------------------
# cmd_create with CLI args
# ---------------------------------------------------------------------------

def test_cmd_create_with_cli_args(tmp_path, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "templates" / "vscode").mkdir(parents=True)
    config = config_with_remotes

    text_calls = []
    def mock_text(message):
        text_calls.append(message)
        mock = MagicMock()
        mock.ask.return_value = ""
        return mock

    def mock_checkbox(message, choices=None, **kwargs):
        mock = MagicMock()
        mock.ask.return_value = ["common"] if "Templates" in message else ["community"]
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "my-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(
            config,
            name="my-ws",
            templates=["common"],
            repos={"community": BranchSpec("origin/master", "master-my-ws")},
        )

    assert not any("Workspace name" in m for m in text_calls)
    assert not any("branch spec" in m for m in text_calls)


def test_cmd_create_rejects_invalid_template(tmp_path, capsys, config):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="test", templates=["nonexistent"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown template" in captured.err.lower()
    assert "common" in captured.err


def test_cmd_create_rejects_invalid_repo_alias(tmp_path, capsys, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    config = config_with_remotes
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="test", repos={"unknown": BranchSpec("origin/master")})
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "unknown repo alias" in captured.err.lower()
    assert "community" in captured.err


def test_cmd_create_rejects_existing_workspace(tmp_path, capsys, config):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "workspaces" / "parrot").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="parrot")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err.lower()


def test_cmd_create_rejects_invalid_name(tmp_path, capsys, config):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="bad name!")
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "alphanumeric" in captured.err.lower()


def test_cmd_create_rejects_duplicate_branch(tmp_path, capsys, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    existing_ws = tmp_path / "workspaces" / "parrot"
    existing_ws.mkdir(parents=True)
    ow_config = existing_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..master-parrot"\n')
    config = config_with_remotes

    def mock_checkbox(message, choices=None, **kwargs):
        mock = MagicMock()
        mock.ask.return_value = ["common"] if "Templates" in message else ["community"]
        return mock

    def mock_text(message):
        mock = MagicMock()
        mock.ask.return_value = ""
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-parrot")})

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "already uses" in captured.err.lower()
    assert "master-parrot" in captured.err


def test_cmd_create_accepts_different_branch(tmp_path, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    existing_ws = tmp_path / "workspaces" / "parrot"
    existing_ws.mkdir(parents=True)
    ow_config = existing_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..master-parrot"\n')
    config = config_with_remotes

    with (
        patch("questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in kw.get("message", "") else ["community"])),
        patch("questionary.text", return_value=MagicMock(ask=lambda: "")),
        patch("questionary.confirm", return_value=MagicMock(ask=lambda: True)),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates") as mock_apply,
        patch("ow.commands.create.write_workspace_config") as mock_write,
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-new")})

    mock_apply.assert_called_once()
    mock_write.assert_called_once()


def test_cmd_create_configuration_duplicates(tmp_path, config_with_remotes):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "templates" / "vscode").mkdir(parents=True)
    src_ws = tmp_path / "workspaces" / "parrot"
    src_ws.mkdir(parents=True)
    ow_config = src_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text(
        'templates = ["common", "vscode"]\n\n'
        '[repos]\ncommunity = "master..master-parrot"\n\n'
        '[vars]\nhttp_port = 9000\n'
    )
    config = _make_config(
        root_dir=tmp_path,
        vars={"http_port": 8069},
        remotes=config_with_remotes.remotes,
    )

    checkbox_calls = []
    def mock_checkbox(message, choices=None, **kwargs):
        checkbox_calls.append({"message": message, "choices": choices})
        mock = MagicMock()
        mock.ask.return_value = ["common", "vscode"] if "Templates" in message else ["community"]
        return mock

    def mock_text(message):
        mock = MagicMock()
        mock.ask.return_value = ""
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config, name="new-ws", repos={"community": BranchSpec("origin/master", "master-new")}, configuration=str(src_ws))

    template_checkbox = checkbox_calls[0]
    checked = [c.title for c in template_checkbox["choices"] if c.checked]
    assert "common" in checked
    assert "vscode" in checked

    repo_checkbox = checkbox_calls[1]
    checked = [c.title for c in repo_checkbox["choices"] if c.checked]
    assert "community" in checked


def test_cmd_create_configuration_rejects_unknown_remote(tmp_path, capsys):
    (tmp_path / "templates" / "common").mkdir(parents=True)
    src_ws = tmp_path / "workspaces" / "parrot"
    src_ws.mkdir(parents=True)
    ow_config = src_ws / ".ow" / "config"
    ow_config.parent.mkdir(parents=True)
    ow_config.write_text(
        'templates = ["common"]\n\n'
        '[repos]\ncommunity = "master"\nenterprise = "master"\n'
    )
    config = _make_config(
        root_dir=tmp_path,
        remotes={"community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")}},
    )

    with pytest.raises(SystemExit) as exc:
        cmd_create(config, name="new-ws", configuration=str(src_ws))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "enterprise" in captured.err.lower()
    assert "not defined" in captured.err.lower()
    assert "community" in captured.err


# ---------------------------------------------------------------------------
# cmd_create — checkbox uses Choice objects
# ---------------------------------------------------------------------------

def test_cmd_create_checkbox_uses_choice_objects(tmp_path, config):
    """cmd_create must use questionary.Choice objects, none selected by default."""
    (tmp_path / "templates" / "zed").mkdir(parents=True)
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "templates" / "vscode").mkdir(parents=True)
    config.remotes = {
        "brboi-addons": {"origin": MagicMock(url="git@github.com:brboi/addons.git")},
        "community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")},
    }

    checkbox_calls = []

    def mock_checkbox(message, choices=None, **kwargs):
        checkbox_calls.append({"message": message, "choices": choices})
        mock = MagicMock()
        if "Templates" in message:
            mock.ask.return_value = ["common", "vscode"]
        else:
            mock.ask.return_value = ["brboi-addons", "community"]
        return mock

    def mock_text(message):
        mock = MagicMock()
        if "Workspace name" in message:
            mock.ask.return_value = "test"
        elif "branch spec" in message:
            mock.ask.return_value = "master"
        else:
            mock.ask.return_value = ""
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "test", {"brboi-addons", "community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config)

    # Verify templates are alphabetical and unchecked
    template_checkbox = checkbox_calls[0]
    assert "Templates" in template_checkbox["message"]
    template_names = [c.title for c in template_checkbox["choices"]]
    assert template_names == ["common", "vscode", "zed"]  # alphabetical
    for choice in template_checkbox["choices"]:
        assert not choice.checked  # none selected by default

    # Verify remotes are in declaration order and unchecked
    repo_checkbox = checkbox_calls[1]
    assert "Repos" in repo_checkbox["message"]
    repo_names = [c.title for c in repo_checkbox["choices"]]
    assert repo_names == ["brboi-addons", "community"]  # declaration order, not sorted
    for choice in repo_checkbox["choices"]:
        assert not choice.checked  # none selected by default


def test_cmd_create_rejects_existing_workspace_interactive(tmp_path, config):
    """cmd_create loops when workspace name already exists."""
    (tmp_path / "templates" / "common").mkdir(parents=True)
    (tmp_path / "workspaces" / "parrot").mkdir(parents=True)
    config.remotes = {"community": {"origin": MagicMock(url="git@github.com:odoo/odoo.git")}}

    call_count = [0]

    def mock_checkbox(message, choices=None, **kwargs):
        mock = MagicMock()
        if "Templates" in message:
            mock.ask.return_value = ["common"]
        else:
            mock.ask.return_value = ["community"]
        return mock

    def mock_text(message):
        if "Workspace name" in message:
            call_count[0] += 1
            mock = MagicMock()
            if call_count[0] == 1:
                mock.ask.return_value = "parrot"  # already exists
            else:
                mock.ask.return_value = "new-ws"  # valid
        elif "branch spec" in message:
            mock = MagicMock()
            mock.ask.return_value = "master"
        else:
            mock = MagicMock()
            mock.ask.return_value = ""
        return mock

    def mock_confirm(message):
        mock = MagicMock()
        mock.ask.return_value = True
        return mock

    with (
        patch("questionary.checkbox", side_effect=mock_checkbox),
        patch("questionary.text", side_effect=mock_text),
        patch("questionary.confirm", side_effect=mock_confirm),
        patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "new-ws", {"community"}, {})),
        patch("ow.commands.create.apply_templates"),
        patch("ow.commands.create.write_workspace_config"),
        patch("ow.commands.create.run_cmd"),
    ):
        cmd_create(config)

    assert call_count[0] == 2  # asked twice: first name rejected, second accepted


# ---------------------------------------------------------------------------
# _cleanup_failed_workspace
# ---------------------------------------------------------------------------

def test_cleanup_failed_workspace_removes_if_empty(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    _cleanup_failed_workspace(ws_dir)
    assert not ws_dir.exists()


def test_cleanup_failed_workspace_removes_if_only_ow_dir(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / ".ow").mkdir(parents=True)
    _cleanup_failed_workspace(ws_dir)
    assert not ws_dir.exists()


def test_cleanup_failed_workspace_keeps_if_has_files(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    (ws_dir / "somefile.txt").touch()
    _cleanup_failed_workspace(ws_dir)
    assert ws_dir.exists()
    assert (ws_dir / "somefile.txt").exists()


def test_cleanup_failed_workspace_does_nothing_if_not_exists(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    _cleanup_failed_workspace(ws_dir)  # should not raise
    assert not ws_dir.exists()


# ---------------------------------------------------------------------------
# _check_duplicate_branches
# ---------------------------------------------------------------------------

def test_check_duplicate_branches_detects_same_local_branch(tmp_path, capsys, config):
    """Abort if new repo shares local_branch with existing workspace on same alias."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    (existing_ws / ".ow").mkdir(parents=True)
    existing_config = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "shared-branch")},
        templates=["common"],
    )
    write_workspace_config(existing_ws / ".ow" / "config", existing_config)

    new_repos = {"community": BranchSpec("origin/master", "shared-branch")}

    with pytest.raises(SystemExit) as exc_info:
        _check_duplicate_branches(new_repos, config)
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "existing" in captured.err
    assert "shared-branch" in captured.err


def test_check_duplicate_branches_no_duplicate_if_different_local_branch(tmp_path, capsys, config):
    """No abort if local_branch differs."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    (existing_ws / ".ow").mkdir(parents=True)
    existing_config = WorkspaceConfig(
        repos={"community": BranchSpec("origin/master", "other-branch")},
        templates=["common"],
    )
    write_workspace_config(existing_ws / ".ow" / "config", existing_config)

    new_repos = {"community": BranchSpec("origin/master", "my-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert "Error" not in captured.err


def test_check_duplicate_branches_no_duplicate_if_different_alias(tmp_path, capsys, config):
    """No abort if alias differs — git allows same branch on different aliases."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    (existing_ws / ".ow").mkdir(parents=True)
    existing_config = WorkspaceConfig(
        repos={"enterprise": BranchSpec("origin/master", "shared-branch")},
        templates=["common"],
    )
    write_workspace_config(existing_ws / ".ow" / "config", existing_config)

    new_repos = {"community": BranchSpec("origin/master", "shared-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert "Error" not in captured.err


def test_check_duplicate_branches_ignores_workspaces_without_ow_config(tmp_path, capsys, config):
    """Skip workspaces that have no .ow/config file."""
    ws_root = tmp_path / "workspaces"
    existing_ws = ws_root / "existing"
    existing_ws.mkdir(parents=True)
    # No .ow/config created

    new_repos = {"community": BranchSpec("origin/master", "some-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert "Error" not in captured.err


def test_check_duplicate_branches_silent_if_no_existing_workspaces(tmp_path, capsys, config):
    """Return silently when no workspaces exist yet."""
    new_repos = {"community": BranchSpec("origin/master", "some-branch")}

    _check_duplicate_branches(new_repos, config)  # should not raise

    captured = capsys.readouterr()
    assert captured.err == ""
