import pytest

from ow.commands.init import _resolve_target
from ow.utils.config import BranchSpec


class TestResolveTarget:

    def test_rejects_unknown_repo_alias(self, tmp_path, monkeypatch, capsys, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _resolve_target(
                config_with_remotes, "test", {"bad": BranchSpec("origin/master")}, configuration=None,
            )
        assert exc.value.code == 1
        assert "unknown repo alias" in capsys.readouterr().err.lower()

    def test_loads_the_configuration_it_is_pointed_at(self, tmp_path, monkeypatch, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        src_config = tmp_path / "src" / ".ow" / "config.toml"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('[repos]\ncommunity = "master..my-branch"\n')

        source_ws, ws_dir, existing = _resolve_target(
            config_with_remotes, "test", None, configuration=str(tmp_path / "src"),
        )

        assert source_ws is not None
        assert source_ws.repos["community"].local_branch == "my-branch"
        assert ws_dir == tmp_path / "test"
        assert existing is False

    def test_accepts_a_configuration_given_as_a_file(self, tmp_path, monkeypatch, config_with_remotes):
        """-c takes the workspace directory or the config file itself."""
        monkeypatch.chdir(tmp_path)
        src_config = tmp_path / "src" / ".ow" / "config.toml"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('[repos]\ncommunity = "master..my-branch"\n')

        source_ws, _, _ = _resolve_target(
            config_with_remotes, "test", None, configuration=str(src_config),
        )

        assert source_ws is not None
        assert source_ws.repos["community"].local_branch == "my-branch"

    def test_rejects_a_configuration_naming_an_unknown_remote(self, tmp_path, monkeypatch, capsys, config_with_remotes):
        monkeypatch.chdir(tmp_path)
        src_config = tmp_path / "src" / ".ow" / "config.toml"
        src_config.parent.mkdir(parents=True)
        src_config.write_text('[repos]\nghost = "master"\n')

        with pytest.raises(SystemExit) as exc:
            _resolve_target(config_with_remotes, "test", None, configuration=str(tmp_path / "src"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "ghost" in err
        assert "not defined in [remotes]" in err

    def test_rejects_a_blank_name(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _resolve_target(config, "  ", None, configuration=None)
        assert exc.value.code == 1
        assert "alphanumeric" in capsys.readouterr().err.lower()

    def test_reports_existing_when_the_marker_is_already_there(self, tmp_path, monkeypatch, config):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ws" / ".ow").mkdir(parents=True)
        (tmp_path / "ws" / ".ow" / "config.toml").write_text('version = 2\n[repos]\n')

        _, ws_dir, existing = _resolve_target(config, "ws", None, configuration=None)

        assert ws_dir == tmp_path / "ws"
        assert existing is True

    def test_configuration_not_found_names_the_path(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _resolve_target(config, "test", None, configuration=str(tmp_path / "missing"))
        assert exc.value.code == 1
        assert str(tmp_path / "missing") in capsys.readouterr().err
