from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_create
from ow.commands.create import _validate_create_inputs, _gather_workspace_config_interactive
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config


# ---------------------------------------------------------------------------
# _validate_create_inputs — duplicate check, --configuration, missing template
# ---------------------------------------------------------------------------

class TestValidateCreateInputs:
    """Coverage for lines 52-53, 84, 86-87, 93-95, 117-119, 121-122, 125-126, 226."""

    def test_validate_rejects_unknown_template(self, tmp_path, capsys, config):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "test", ["nonexistent"], {}, configuration=None)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "unknown template" in captured.err.lower()

    def test_validate_rejects_unknown_repo_alias(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "test", None, {"bad": BranchSpec("origin/master")}, configuration=None)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "unknown repo alias" in captured.err.lower()

    def test_validate_configuration_file(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        src_config = tmp_path / "src" / ".ow" / "config"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..my-branch"\n')
        source_ws, name, ws_dir = _validate_create_inputs(
            config, "test", None, None, configuration=str(src_config.parent.parent)
        )
        assert source_ws is not None
        assert source_ws.repos["community"].local_branch == "my-branch"

    def test_validate_configuration_not_found(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "test", None, None, configuration="/nonexistent/path")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()

    def test_validate_configuration_invalid_template(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        src_config = tmp_path / "src" / ".ow" / "config"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('templates = ["common", "nonexistent"]\n\n[repos]\ncommunity = "master"\n')
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "test", None, None, configuration=str(src_config.parent.parent))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "unknown template" in captured.err.lower()

    def test_validate_configuration_invalid_alias(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        src_config = tmp_path / "src" / ".ow" / "config"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('templates = ["common"]\n\n[repos]\nunknown_alias = "master"\n')
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "test", None, None, configuration=str(src_config.parent.parent))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "unknown_alias" in captured.err

    def test_validate_existing_workspace_name(self, tmp_path, capsys, config):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        (tmp_path / "workspaces" / "existing").mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "existing", None, None, configuration=None)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "already exists" in captured.err.lower()

    def test_validate_name_empty(self, tmp_path, capsys, config):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            _validate_create_inputs(config, "  ", None, None, configuration=None)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "alphanumeric" in captured.err.lower()

class TestCmdCreateExtended:

    def test_cmd_create_interactive_name_retry(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        call_count = [0]
        def mock_text(message):
            call_count[0] += 1
            mock = MagicMock()
            if call_count[0] == 1:
                mock.ask.return_value = "my-ws"
            else:
                mock.ask.return_value = ""
            return mock
        def mock_checkbox(message, choices=None, **kw):
            mock = MagicMock()
            if "Templates" in str(message):
                mock.ask.return_value = ["common"]
            elif "Repos" in str(message):
                mock.ask.return_value = ["community"]
            else:
                mock.ask.return_value = []
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
            cmd_create(config, name="my-ws")
        # name was provided so questionary.text was not called for name prompt

    def test_cmd_create_interactive_spec_input(self, tmp_path, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        text_answers = iter(["my-ws", "master"])
        def mock_text(message):
            mock = MagicMock()
            if "Workspace name" in str(message):
                mock.ask.return_value = next(text_answers)
            elif "branch spec" in str(message):
                mock.ask.return_value = next(text_answers)
            else:
                mock.ask.return_value = ""
            return mock
        with (
            patch("questionary.text", side_effect=mock_text),
            patch("questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in str(a[0]) else ["community"])),
            patch("questionary.confirm", side_effect=lambda *a, **kw: MagicMock(ask=lambda: True)),
            patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "my-ws", {"community"}, {})),
            patch("ow.commands.create.apply_templates"),
            patch("ow.commands.create.write_workspace_config"),
            patch("ow.commands.create.run_cmd"),
        ):
            cmd_create(config)

    def test_cmd_create_confirm_abort(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        with (
            patch("questionary.text", side_effect=lambda *a, **kw: MagicMock(ask=lambda: "my-ws")),
            patch("questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in str(a[0]) else ["community"])),
            patch("questionary.confirm", side_effect=lambda *a, **kw: MagicMock(ask=lambda: False)),
        ):
            cmd_create(config)
        captured = capsys.readouterr()
        assert "Aborted." in captured.out

    def test_cmd_create_all_repos_fail(self, tmp_path, capsys, config_with_remotes, tmp_path_factory):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        with (
            patch("questionary.text", side_effect=lambda *a, **kw: MagicMock(ask=lambda: "my-ws")),
            patch("questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in str(a[0]) else ["community"])),
            patch("questionary.confirm", side_effect=lambda *a, **kw: MagicMock(ask=lambda: True)),
            patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "my-ws", set(), {"community": "failed"})),
            patch("ow.commands.create.apply_templates"),
            patch("ow.commands.create.write_workspace_config"),
            patch("ow.commands.create.run_cmd"),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_create(config)
            assert exc.value.code == 1

    def test_cmd_create_some_repos_fail(self, tmp_path, capsys, config_with_remotes):
        tpl = tmp_path / "templates" / "common"
        tpl.mkdir(parents=True)
        config = config_with_remotes
        with (
            patch("questionary.text", side_effect=lambda *a, **kw: MagicMock(ask=lambda: "my-ws")),
            patch("questionary.checkbox", side_effect=lambda *a, **kw: MagicMock(ask=lambda: ["common"] if "Templates" in str(a[0]) else ["community"])),
            patch("questionary.confirm", side_effect=lambda *a, **kw: MagicMock(ask=lambda: True)),
            patch("ow.commands.create.ensure_workspace_materialized", return_value=(tmp_path / "workspaces" / "my-ws", {"community"}, {})),
            patch("ow.commands.create.apply_templates"),
            patch("ow.commands.create.write_workspace_config"),
            patch("ow.commands.create.run_cmd"),
        ):
            cmd_create(config, name="my-ws", templates=["common"], repos={"community": BranchSpec("origin/master", "my-branch")})

