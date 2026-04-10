from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.templates import (
    _get_packaged_templates,
    _resolve_template_dir,
    available_templates,
    apply_templates,
    ensure_workspace_materialized,
    is_odoo_main_repo,
)


class TestGetPackagedTemplates:
    def test_returns_template_names(self):
        names = _get_packaged_templates()
        assert "common" in names
        assert "vscode" in names
        assert "zed" in names


class TestAvailableTemplates:
    def test_returns_packaged_only(self, tmp_path):
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        names = available_templates(config)
        assert "common" in names
        assert "vscode" in names

    def test_merges_local_and_packaged(self, tmp_path):
        local = tmp_path / "templates" / "my-custom"
        local.mkdir(parents=True)
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        names = available_templates(config)
        assert "my-custom" in names
        assert "common" in names
        assert "vscode" in names

    def test_sorted_order(self, tmp_path):
        for name in ["my-custom"]:
            d = tmp_path / "templates" / name
            d.mkdir(parents=True)
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        names = available_templates(config)
        assert names == sorted(names)


class TestResolveTemplateDir:
    def test_packaged_template(self, tmp_path):
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        result = _resolve_template_dir("common", config)
        assert result.is_dir()

    def test_local_template_takes_priority(self, tmp_path):
        local = tmp_path / "templates" / "common"
        local.mkdir(parents=True)
        (local / "custom.txt").write_text("local")
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        result = _resolve_template_dir("common", config)
        assert result == local

    def test_missing_template_raises(self, tmp_path):
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        with pytest.raises(FileNotFoundError, match="not found"):
            _resolve_template_dir("nonexistent-template", config)


class TestIsOdooMainRepo:
    def test_true(self, tmp_path):
        repo = tmp_path / "community"
        repo.mkdir(parents=True)
        (repo / "odoo-bin").touch()
        (repo / "addons").mkdir(parents=True)
        (repo / "odoo" / "addons").mkdir(parents=True)
        assert is_odoo_main_repo(repo) is True

    def test_false_no_odoo_bin(self, tmp_path):
        repo = tmp_path / "enterprise"
        (repo / "addons").mkdir(parents=True)
        assert is_odoo_main_repo(repo) is False

    def test_false_no_addons(self, tmp_path):
        repo = tmp_path / "odoo"
        repo.mkdir(parents=True)
        (repo / "odoo-bin").touch()
        assert is_odoo_main_repo(repo) is False


class TestApplyTemplates:
    def _make_main_repo(self, ws_dir):
        repo = ws_dir / "community"
        repo.mkdir()
        (repo / "odoo-bin").touch()
        (repo / "addons").mkdir(parents=True)
        (repo / "odoo" / "addons").mkdir(parents=True)

    def test_applies_common_to_workspace(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        apply_templates(ws, config, ws_dir)

        assert (ws_dir / "odoorc").exists()
        assert (ws_dir / "pyrightconfig.json").exists()
        assert (ws_dir / "requirements-dev.txt").exists()

    def test_template_uses_context(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "my-test-ws"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common"])
        apply_templates(ws, config, ws_dir)
        odoorc = (ws_dir / "odoorc").read_text()
        assert "my-test-ws" in odoorc

    def test_applies_with_main_repo_context(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        self._make_main_repo(ws_dir)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        apply_templates(ws, config, ws_dir)
        odoorc = (ws_dir / "odoorc").read_text()
        assert "community/addons" in odoorc

    def test_applies_vscode(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        self._make_main_repo(ws_dir)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common", "vscode"])
        apply_templates(ws, config, ws_dir)
        assert (ws_dir / ".vscode" / "settings.json").exists()
        assert (ws_dir / ".vscode" / "launch.json").exists()

    def test_later_template_overrides_earlier(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        self._make_main_repo(ws_dir)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common", "vscode"])
        apply_templates(ws, config, ws_dir)
        assert (ws_dir / "odoorc").exists()


class TestEnsureWorkspaceMaterialized:
    def test_no_repos_returns_empty(self, tmp_path, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        config = config_with_remotes
        ws = WorkspaceConfig(repos={}, templates=["common"])
        result_dir, successful, errors = ensure_workspace_materialized(ws, config, ws_dir)
        assert result_dir == ws_dir
        assert successful == set()
        assert errors == {}

    def test_attaches_detached_worktree(self, tmp_path, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = config_with_remotes
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master", "my-feature")}, templates=["common"])

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mock_resolve:
                mock_resolve.return_value = BranchSpec("origin/master", "my-feature")
                with patch("ow.utils.templates.parallel_per_repo", return_value={"community": BranchSpec("origin/master", "my-feature")}):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch("ow.utils.templates.worktree_is_detached", return_value=True):
                            with patch("ow.utils.templates.attach_worktree") as mock_attach:
                                with patch("ow.utils.templates.run_cmd"):
                                    ensure_workspace_materialized(ws, config, ws_dir)
        mock_attach.assert_called_once()

    def test_detaches_attached_worktree(self, tmp_path, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = config_with_remotes
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mock_resolve:
                mock_resolve.return_value = BranchSpec("origin/master")
                with patch("ow.utils.templates.parallel_per_repo", return_value={"community": BranchSpec("origin/master")}):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch("ow.utils.templates.worktree_is_detached", return_value=False):
                            with patch("ow.utils.templates.detach_worktree") as mock_detach:
                                with patch("ow.utils.templates.run_cmd"):
                                    ensure_workspace_materialized(ws, config, ws_dir)
        mock_detach.assert_called_once()

    def test_sets_upstream_when_attached(self, tmp_path, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = config_with_remotes
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master", "my-feature")}, templates=["common"])

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mock_resolve:
                mock_resolve.return_value = BranchSpec("origin/master", "my-feature")
                with patch("ow.utils.templates.parallel_per_repo", return_value={"community": BranchSpec("origin/master", "my-feature")}):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch("ow.utils.templates.worktree_is_detached", return_value=False):
                            with patch("ow.utils.templates.set_branch_upstream") as mock_upstream:
                                with patch("ow.utils.templates.run_cmd"):
                                    ensure_workspace_materialized(ws, config, ws_dir)
        mock_upstream.assert_called_once()

    def test_creates_new_worktree(self, tmp_path, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = config_with_remotes
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])

        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mock_resolve:
                mock_resolve.return_value = BranchSpec("origin/master")
                with patch("ow.utils.templates.parallel_per_repo", return_value={"community": BranchSpec("origin/master")}):
                    with patch("ow.utils.templates.worktree_exists", return_value=False):
                        with patch("ow.utils.templates.run_cmd"):
                            with patch("ow.utils.templates.create_worktree") as mock_create:
                                ensure_workspace_materialized(ws, config, ws_dir)
        mock_create.assert_called_once()
