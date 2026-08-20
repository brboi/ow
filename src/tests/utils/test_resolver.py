import textwrap

import pytest

from ow.utils.resolver import resolve_workspace
from ow.utils.config import (
    WorkspaceConfig,
    load_config,
    write_workspace_config,
)


def _make_project(root, alias="community"):
    """Create an ow.toml-based config on disk and return it loaded.

    Only used here to build a realistic Config for the tests below — config
    is global now, so this no longer represents "a project" the way it used
    to.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "ow.toml").write_text(
        f'[remotes]\n{alias}.origin.url = "git@github.com:odoo/{alias}.git"\n'
    )
    return load_config(root / "ow.toml")


def _make_ws(project_root, name):
    ws_dir = project_root / "workspaces" / name
    (ws_dir / ".ow").mkdir(parents=True)
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(templates=["common"], repos={}, vars={}),
    )
    return ws_dir


class TestResolveWorkspace:
    def test_env_var_resolution(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ow_config = ws_dir / ".ow" / "config.toml"
        ow_config.parent.mkdir(parents=True)
        ow_config.write_text(textwrap.dedent("""\
            templates = ["common"]

            [repos]
            community = "master"
        """))

        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("OW_WORKSPACE", str(ws_dir))
            _, resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir
        assert ws.templates == ["common"]

    def test_env_var_as_workspace_name_fails_loudly(self, tmp_path, monkeypatch, capsys, config):
        """Name-based lookup has no meaning without the discovery index (task 5)."""
        monkeypatch.setenv("OW_WORKSPACE", "named-ws")

        with pytest.raises(SystemExit):
            resolve_workspace(config)

        err = capsys.readouterr().err
        assert "OW_WORKSPACE" in err
        assert "named-ws" in err
        assert "path instead" in err

    def test_env_var_as_path_outside_workspaces_dir(self, tmp_path, monkeypatch, config):
        ws_dir = tmp_path / "elsewhere" / "my-ws"
        (ws_dir / ".ow").mkdir(parents=True)
        write_workspace_config(
            ws_dir / ".ow" / "config.toml",
            WorkspaceConfig(templates=["common"], repos={}, vars={}),
        )

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        _, resolved_dir, _ = resolve_workspace(config)

        assert resolved_dir == ws_dir

    def test_cwd_walkup(self, tmp_path, monkeypatch, config):
        """resolve_workspace walks up from cwd to find .ow/config.toml."""
        ws_dir = _make_ws(tmp_path, "walkup")
        subdir = ws_dir / "community" / "odoo"
        subdir.mkdir(parents=True)

        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        monkeypatch.chdir(subdir)
        _, resolved_dir, _ = resolve_workspace(config)

        assert resolved_dir == ws_dir

    def test_exits_when_no_workspace_found(self, tmp_path, monkeypatch, config):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit):
            resolve_workspace(config)

    def test_workspace_config_is_named_config_toml(self, tmp_path, monkeypatch, config):
        """Regression guard: the per-workspace config file is `.ow/config.toml`,
        not the old extensionless `.ow/config`. Uses the path form of
        resolution (cwd walk-up) since name-based lookup has no meaning
        without the discovery index (see the tests below)."""
        ws_dir = _make_ws(tmp_path, "toml-check")

        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        monkeypatch.chdir(ws_dir)
        _, resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir
        assert ws.templates == ["common"]

    def test_resolve_workspace_by_name_fails_loudly(self, tmp_path, monkeypatch, capsys, config):
        """Positional name lookup has no meaning without the discovery index (task 5)."""
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        _make_ws(tmp_path, "test")

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="test")

        err = capsys.readouterr().err
        assert "cannot resolve workspace 'test'" in err
        assert "path instead" in err

    def test_resolve_workspace_by_name_not_found(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="nonexistent")

        err = capsys.readouterr().err
        assert "cannot resolve workspace 'nonexistent'" in err
        assert "path instead" in err

    def test_resolve_workspace_by_name_invalid(self, tmp_path, monkeypatch, capsys, config):
        """A directory that isn't even a valid workspace fails the same loud way."""
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        (tmp_path / "workspaces" / "invalid").mkdir(parents=True)

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="invalid")

        err = capsys.readouterr().err
        assert "cannot resolve workspace 'invalid'" in err


class TestConfigIsGlobal:
    """Configuration is global now — there is only ever one Config, and
    resolve_workspace never swaps it out for "the project owning this
    workspace" the way the old per-project scheme did."""

    def test_path_form_returns_the_same_config_object(self, tmp_path, monkeypatch, config):
        ws_dir = _make_ws(tmp_path, "quattromori")

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        cfg, resolved_dir, _ = resolve_workspace(config)

        assert cfg is config
        assert resolved_dir == ws_dir

    def test_cwd_walkup_returns_the_same_config_object(self, tmp_path, monkeypatch, config):
        ws_dir = _make_ws(tmp_path, "walkup")

        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        monkeypatch.chdir(ws_dir)
        cfg, resolved_dir, _ = resolve_workspace(config)

        assert cfg is config
        assert resolved_dir == ws_dir


class TestOwWorkspaceFailsLoudly:
    """One meaning per form, one failure per form — never a silent fallback."""

    def test_unknown_name_via_env_var_fails_with_a_clear_message(self, tmp_path, monkeypatch, capsys):
        project = _make_project(tmp_path / "devrepo", alias="owl")
        monkeypatch.setenv("OW_WORKSPACE", "quattromori")

        with pytest.raises(SystemExit):
            resolve_workspace(project)

        err = capsys.readouterr().err
        assert "OW_WORKSPACE" in err
        assert "quattromori" in err
        assert "path instead" in err

    def test_path_without_ow_config_names_the_env_var(self, tmp_path, monkeypatch, capsys):
        project = _make_project(tmp_path / "devrepo", alias="owl")
        stray = tmp_path / "not-a-workspace"
        stray.mkdir()

        monkeypatch.setenv("OW_WORKSPACE", str(stray))
        with pytest.raises(SystemExit):
            resolve_workspace(project)

        err = capsys.readouterr().err
        assert "OW_WORKSPACE" in err
        assert str(stray) in err

    def test_bare_name_never_falls_back_to_a_relative_path(self, tmp_path, monkeypatch, capsys):
        """The old code fell through to Path(env_val)/.ow/config.toml, relative to cwd."""
        project = _make_project(tmp_path / "devrepo", alias="owl")
        decoy = tmp_path / "cwd" / "quattromori"
        (decoy / ".ow").mkdir(parents=True)
        write_workspace_config(
            decoy / ".ow" / "config.toml",
            WorkspaceConfig(templates=[], repos={}, vars={}),
        )

        monkeypatch.chdir(tmp_path / "cwd")
        monkeypatch.setenv("OW_WORKSPACE", "quattromori")

        with pytest.raises(SystemExit):
            resolve_workspace(project)
