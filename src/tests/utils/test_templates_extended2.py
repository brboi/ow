from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.templates import (
    _get_packaged_templates,
    _resolve_template_dir,
    apply_templates,
    ensure_workspace_materialized,
)


class TestEnsureWorkspaceMaterializedExtended:

    def test_existing_not_detached_not_changed(self, tmp_path, config_with_remotes):
        """Worktree exists, not detached, resolved is attached — only set_branch_upstream called."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = config_with_remotes
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master", "my-feature")}, templates=["common"])
        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.return_value = BranchSpec("origin/master", "my-feature")
                with patch("ow.utils.templates.parallel_per_repo", return_value={"community": BranchSpec("origin/master", "my-feature")}):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch("ow.utils.templates.worktree_is_detached", return_value=False):
                            with patch("ow.utils.templates.set_branch_upstream") as mock_up:
                                with patch("ow.utils.templates.run_cmd"):
                                    ensure_workspace_materialized(ws, config, ws_dir)
        mock_up.assert_called_once()

    def test_existing_detached_and_resolved_detached_noop(self, tmp_path, config_with_remotes):
        """Both detached — no worktree modification needed."""
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        config = config_with_remotes
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common"])
        with patch("ow.utils.templates.ensure_bare_repo"):
            with patch("ow.utils.templates.resolve_spec") as mr:
                mr.return_value = BranchSpec("origin/master")
                with patch("ow.utils.templates.parallel_per_repo", return_value={"community": BranchSpec("origin/master")}):
                    with patch("ow.utils.templates.worktree_exists", return_value=True):
                        with patch("ow.utils.templates.worktree_is_detached", return_value=True):
                            with patch("ow.utils.templates.run_cmd") as mock_cmd:
                                with patch("ow.utils.templates.attach_worktree") as mock_attach:
                                    with patch("ow.utils.templates.detach_worktree") as mock_detach:
                                        with patch("ow.utils.templates.set_branch_upstream") as mock_up:
                                            ensure_workspace_materialized(ws, config, ws_dir)
        mock_attach.assert_not_called()
        mock_detach.assert_not_called()
        mock_up.assert_not_called()


class TestResolveTemplateDirExtended:

    def test_packaged_template_via_config(self, tmp_path):
        config = Config(vars={}, remotes={}, root_dir=tmp_path)
        result = _resolve_template_dir("zed", config)
        assert result.is_dir()


class TestApplyTemplatesExtended:

    def _make_main_repo(self, ws_dir):
        repo = ws_dir / "community"
        repo.mkdir()
        (repo / "odoo-bin").touch()
        (repo / "addons").mkdir(parents=True)
        (repo / "odoo" / "addons").mkdir(parents=True)

    def test_applies_zed(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        self._make_main_repo(ws_dir)
        ws = WorkspaceConfig(repos={"community": BranchSpec("origin/master")}, templates=["common", "zed"])
        apply_templates(ws, config, ws_dir)
        assert (ws_dir / ".zed" / "settings.json").exists()
        assert (ws_dir / ".zed" / "debug.json").exists()

    def test_applies_bwrap(self, tmp_path, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(repos={}, templates=["common", "bwrap"])
        apply_templates(ws, config, ws_dir)
        assert (ws_dir / "bwrap-opencode").exists()
