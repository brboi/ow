from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_init
from ow.commands.init import _validate_init_inputs
from ow.utils import paths
from ow.utils.config import BranchSpec


# ---------------------------------------------------------------------------
# _validate_init_inputs — the checks that run before anything is asked
# ---------------------------------------------------------------------------

class TestValidateInitInputs:

    def test_rejects_unknown_template(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.chdir(tmp_path)
        (paths.templates_dir() / "common").mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            _validate_init_inputs(config, "test", ["nonexistent"], {}, configuration=None)
        assert exc.value.code == 1
        assert "unknown template" in capsys.readouterr().err.lower()

    def test_rejects_unknown_repo_alias(self, tmp_path, monkeypatch, capsys, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _validate_init_inputs(
                config_with_remotes, "test", None, {"bad": BranchSpec("origin/master")}, configuration=None
            )
        assert exc.value.code == 1
        assert "unknown repo alias" in capsys.readouterr().err.lower()

    def test_loads_the_configuration_it_is_pointed_at(self, tmp_path, monkeypatch, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        src_config = tmp_path / "src" / ".ow" / "config.toml"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..my-branch"\n')

        source_ws, ws_dir = _validate_init_inputs(
            config_with_remotes, "test", None, None, configuration=str(tmp_path / "src")
        )

        assert source_ws is not None
        assert source_ws.repos["community"].local_branch == "my-branch"
        assert ws_dir == tmp_path / "test"

    def test_accepts_a_configuration_given_as_a_file(self, tmp_path, monkeypatch, config_with_remotes):
        """-c takes the workspace directory or the config file itself."""
        monkeypatch.chdir(tmp_path)
        src_config = tmp_path / "src" / ".ow" / "config.toml"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('templates = ["common"]\n\n[repos]\ncommunity = "master..my-branch"\n')

        source_ws, _ = _validate_init_inputs(
            config_with_remotes, "test", None, None, configuration=str(src_config)
        )

        assert source_ws is not None
        assert source_ws.repos["community"].local_branch == "my-branch"

    def test_rejects_a_configuration_naming_an_unknown_template(self, tmp_path, monkeypatch, capsys, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        src_config = tmp_path / "src" / ".ow" / "config.toml"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('templates = ["common", "nonexistent"]\n\n[repos]\ncommunity = "master"\n')
        with pytest.raises(SystemExit) as exc:
            _validate_init_inputs(config_with_remotes, "test", None, None, configuration=str(tmp_path / "src"))
        assert exc.value.code == 1
        assert "unknown template" in capsys.readouterr().err.lower()

    def test_rejects_a_blank_name(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _validate_init_inputs(config, "  ", None, None, configuration=None)
        assert exc.value.code == 1
        assert "alphanumeric" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# The questionnaire fills in what the flags left out
# ---------------------------------------------------------------------------

class TestInteractiveQuestionnaire:

    def test_asks_for_the_spec_of_an_alias_no_flag_covered(self, tmp_path, monkeypatch, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        asked = []

        def mock_text(message, **kwargs):
            asked.append(message)
            return MagicMock(ask=lambda: "master..from-the-questionnaire")

        stdin = MagicMock()
        stdin.isatty.return_value = True

        with (
            patch("sys.stdin", stdin),
            patch("questionary.text", side_effect=mock_text),
            patch("questionary.checkbox", side_effect=lambda message, **kw: MagicMock(
                ask=lambda: ["common"] if "Templates" in message else ["community"]
            )),
            patch("questionary.confirm", side_effect=lambda message: MagicMock(ask=lambda: True)),
            patch("ow.commands.init.ensure_workspace_materialized", return_value=(tmp_path, set(), {})),
            patch("ow.commands.init.apply_templates"),
            patch("ow.commands.init.run_cmd"),
        ):
            cmd_init(config_with_remotes)

        assert any("branch spec" in message for message in asked)
        assert "from-the-questionnaire" in (tmp_path / ".ow" / "config.toml").read_text()

    def test_an_empty_spec_takes_the_master_default(self, tmp_path, monkeypatch, config_with_remotes):
        """#38: a cleared prompt asks for the default, not for the command to stop."""
        monkeypatch.chdir(tmp_path)
        defaults = []

        def mock_text(message, **kwargs):
            defaults.append(kwargs.get("default"))
            return MagicMock(ask=lambda: "")

        stdin = MagicMock()
        stdin.isatty.return_value = True

        with (
            patch("sys.stdin", stdin),
            patch("questionary.text", side_effect=mock_text),
            patch("questionary.checkbox", side_effect=lambda message, **kw: MagicMock(
                ask=lambda: ["common"] if "Templates" in message else ["community"]
            )),
            patch("questionary.confirm", side_effect=lambda message: MagicMock(ask=lambda: True)),
            patch("ow.commands.init.ensure_workspace_materialized", return_value=(tmp_path, set(), {})),
            patch("ow.commands.init.apply_templates"),
            patch("ow.commands.init.run_cmd"),
        ):
            cmd_init(config_with_remotes)

        # The prompt is prefilled, so a plain Enter already yields "master".
        assert defaults == ["master"]
        assert 'community = "master"' in (tmp_path / ".ow" / "config.toml").read_text()

    def test_an_interrupted_spec_prompt_still_aborts(self, tmp_path, monkeypatch, capsys, config_with_remotes):
        """questionary returns None on Ctrl-C — that is not an empty answer."""
        monkeypatch.chdir(tmp_path)

        stdin = MagicMock()
        stdin.isatty.return_value = True

        with (
            patch("sys.stdin", stdin),
            patch("questionary.text", side_effect=lambda message, **kw: MagicMock(ask=lambda: None)),
            patch("questionary.checkbox", side_effect=lambda message, **kw: MagicMock(
                ask=lambda: ["common"] if "Templates" in message else ["community"]
            )),
            patch("questionary.confirm", side_effect=lambda message: MagicMock(ask=lambda: True)),
            patch("ow.commands.init.ensure_workspace_materialized", return_value=(tmp_path, set(), {})),
            patch("ow.commands.init.apply_templates"),
            patch("ow.commands.init.run_cmd"),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_init(config_with_remotes)

        assert exc.value.code == 1
        assert "Aborted" in capsys.readouterr().err
