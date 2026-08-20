import textwrap

import pytest

from ow.utils.resolver import resolve_workspace
from ow.utils.config import (
    WorkspaceConfig,
    load_config,
    write_workspace_config,
)


def _make_project(root, alias="community"):
    """Create an ow project on disk and return its loaded Config."""
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

    def test_env_var_as_workspace_name(self, tmp_path, monkeypatch, config):
        ws_dir = _make_ws(tmp_path, "named-ws")
        monkeypatch.setenv("OW_WORKSPACE", "named-ws")
        _, resolved_dir, ws = resolve_workspace(config)

        assert resolved_dir == ws_dir
        assert ws.templates == ["common"]

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

    def test_resolve_workspace_by_name(self, tmp_path, monkeypatch, config):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        ws_dir = _make_ws(tmp_path, "test")

        _, resolved_dir, _ = resolve_workspace(config, name="test")
        assert resolved_dir == ws_dir

    def test_workspace_config_is_named_config_toml(self, tmp_path, monkeypatch, config):
        """The workspace config file is .ow/config.toml.toml, not the old .ow/config.toml (task 3)."""
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        ws_dir = tmp_path / "workspaces" / "toml-check"
        ws_dir.mkdir(parents=True)
        write_workspace_config(
            ws_dir / ".ow" / "config.toml",
            WorkspaceConfig(templates=["common"], repos={}, vars={}),
        )

        _, resolved_dir, ws = resolve_workspace(config, name="toml-check")

        assert resolved_dir == ws_dir
        assert ws.templates == ["common"]

    def test_resolve_workspace_by_name_not_found(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="nonexistent")

        assert "Workspace 'nonexistent' not found" in capsys.readouterr().err

    def test_resolve_workspace_by_name_invalid(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        (tmp_path / "workspaces" / "invalid").mkdir(parents=True)

        with pytest.raises(SystemExit):
            resolve_workspace(config, name="invalid")

        assert "not a valid workspace" in capsys.readouterr().err


class TestProjectRootFollowsTheWorkspace:
    """A workspace outside the current project must be read with its own ow.toml."""

    def test_path_outside_current_project_reroots(self, tmp_path, monkeypatch):
        _make_project(tmp_path / "odoo", alias="community")
        other = _make_project(tmp_path / "devrepo", alias="owl")
        ws_dir = _make_ws(tmp_path / "odoo", "quattromori")

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        cfg, resolved_dir, _ = resolve_workspace(other)

        assert resolved_dir == ws_dir
        assert cfg.root_dir == tmp_path / "odoo"
        assert sorted(cfg.remotes) == ["community"]

    def test_reroot_is_announced_on_stderr(self, tmp_path, monkeypatch, capsys):
        _make_project(tmp_path / "odoo")
        other = _make_project(tmp_path / "devrepo", alias="owl")
        ws_dir = _make_ws(tmp_path / "odoo", "quattromori")

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        resolve_workspace(other)

        captured = capsys.readouterr()
        assert f"Using project {tmp_path / 'odoo'}" in captured.err
        assert captured.out == ""

    def test_workspace_inside_project_keeps_the_config_untouched(self, tmp_path, monkeypatch, capsys):
        project = _make_project(tmp_path / "odoo")
        ws_dir = _make_ws(tmp_path / "odoo", "quattromori")

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        cfg, _, _ = resolve_workspace(project)

        assert cfg is project
        assert capsys.readouterr().err == ""

    def test_orphan_workspace_is_a_hard_failure(self, tmp_path, monkeypatch, capsys):
        other = _make_project(tmp_path / "devrepo", alias="owl")
        ws_dir = tmp_path / "orphan" / "ws"
        (ws_dir / ".ow").mkdir(parents=True)
        write_workspace_config(
            ws_dir / ".ow" / "config.toml",
            WorkspaceConfig(templates=[], repos={}, vars={}),
        )

        monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
        with pytest.raises(SystemExit):
            resolve_workspace(other)

        err = capsys.readouterr().err
        assert "no ow.toml above" in err
        assert str(ws_dir) in err

    def test_positional_name_never_reroots(self, tmp_path, monkeypatch, capsys):
        _make_project(tmp_path / "odoo")
        other = _make_project(tmp_path / "devrepo", alias="owl")
        _make_ws(tmp_path / "odoo", "quattromori")
        _make_ws(tmp_path / "devrepo", "quattromori")

        monkeypatch.delenv("OW_WORKSPACE", raising=False)
        cfg, resolved_dir, _ = resolve_workspace(other, name="quattromori")

        assert cfg is other
        assert resolved_dir == tmp_path / "devrepo" / "workspaces" / "quattromori"
        assert capsys.readouterr().err == ""


class TestOwWorkspaceFailsLoudly:
    """One meaning per form, one failure per form — never a silent fallback."""

    def test_unknown_name_names_the_project_it_looked_in(self, tmp_path, monkeypatch, capsys):
        project = _make_project(tmp_path / "devrepo", alias="owl")
        monkeypatch.setenv("OW_WORKSPACE", "quattromori")

        with pytest.raises(SystemExit):
            resolve_workspace(project)

        err = capsys.readouterr().err
        assert "OW_WORKSPACE" in err
        assert "quattromori" in err
        assert str(tmp_path / "devrepo" / "workspaces") in err

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
