from __future__ import annotations

import textwrap

import pytest

from ow.config import WorkspaceConfig, write_workspace_config
from ow.workspace import resolve_workspace


class TestResolveWorkspace:
    def test_env_var_resolution(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ow_config = ws_dir / ".ow" / "config"
        ow_config.parent.mkdir(parents=True)
        ow_config.write_text(textwrap.dedent("""\
            templates = ["common"]

            [repos]
            community = "master"
        """))

        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("OW_WORKSPACE", str(ws_dir))
            resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir
        assert ws.templates == ["common"]

    def test_env_var_used(self, tmp_path, monkeypatch, config):
        ws_dir = tmp_path / "workspaces" / "env-ws"
        ws_dir.mkdir(parents=True)
        ow_config = ws_dir / ".ow" / "config"
        ow_config.parent.mkdir(parents=True)
        ow_config.write_text(textwrap.dedent("""\
            templates = ["common"]

            [repos]
            community = "master"
        """))

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir

    def test_env_var_as_workspace_name(self, tmp_path, monkeypatch, config):
        ws_dir = tmp_path / "workspaces" / "named-ws"
        ws_dir.mkdir(parents=True)
        ow_config = ws_dir / ".ow" / "config"
        ow_config.parent.mkdir(parents=True)
        ow_config.write_text(textwrap.dedent("""\
            templates = ["common"]

            [repos]
            community = "master"
        """))

        monkeypatch.setenv("OW_WORKSPACE", "named-ws")
        resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir
        assert ws.templates == ["common"]

    def test_env_var_fallback_to_path(self, tmp_path, monkeypatch, config):
        ws_dir = tmp_path / "elsewhere" / "my-ws"
        ws_dir.mkdir(parents=True)
        ow_config = ws_dir / ".ow" / "config"
        ow_config.parent.mkdir(parents=True)
        ow_config.write_text(textwrap.dedent("""\
            templates = ["common"]

            [repos]
            community = "master"
        """))

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir

    def test_cwd_walkup(self, tmp_path, monkeypatch, config):
        """resolve_workspace walks up from cwd to find .ow/config."""
        ws_dir = tmp_path / "workspaces" / "walkup"
        subdir = ws_dir / "community" / "odoo"
        subdir.mkdir(parents=True)
        ow_config = ws_dir / ".ow" / "config"
        ow_config.parent.mkdir(parents=True)
        ow_config.write_text(textwrap.dedent("""\
            templates = ["common"]

            [repos]
            community = "master"
        """))

        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        monkeypatch.chdir(subdir)
        resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir

    def test_exits_when_no_workspace_found(self, tmp_path, monkeypatch, config):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)

        with pytest.raises(SystemExit):
            resolve_workspace(config)

    def test_resolve_workspace_by_name(self, tmp_path, monkeypatch, config):
        """resolve_workspace with name argument resolves to that workspace."""
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / ".ow").mkdir()
        write_workspace_config(ws_dir / ".ow" / "config", WorkspaceConfig(
            templates=["common"], repos={}, vars={}
        ))

        resolved_dir, ws = resolve_workspace(config, name="test")
        assert resolved_dir == ws_dir

    def test_resolve_workspace_by_name_not_found(self, tmp_path, monkeypatch, capsys, config):
        """resolve_workspace with non-existent name exits with error."""
        monkeypatch.delenv("OW_WORKSPACE", raising=False)

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="nonexistent")

        captured = capsys.readouterr()
        assert "Workspace 'nonexistent' not found" in captured.err

    def test_resolve_workspace_by_name_invalid(self, tmp_path, monkeypatch, capsys, config):
        """resolve_workspace with name of non-workspace directory exits with error."""
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        ws_dir = tmp_path / "workspaces" / "invalid"
        ws_dir.mkdir(parents=True)

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="invalid")

        captured = capsys.readouterr()
        assert "not a valid workspace" in captured.err
