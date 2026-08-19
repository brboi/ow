from pathlib import Path

from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.refs import fetch_workspace_refs


def _workspace(tmp_path, alias="community"):
    """A project whose worktree exists but whose bare repo does not."""
    root = tmp_path / "project"
    ws_dir = root / "workspaces" / "ws"
    (ws_dir / alias).mkdir(parents=True)
    config = Config(vars={}, remotes={}, root_dir=root)
    ws = WorkspaceConfig(
        templates=[], vars={}, repos={alias: BranchSpec("origin/master", "feature")}
    )
    return config, ws, ws_dir


class TestMissingBareRepo:
    """A missing bare repo is a broken project, not a missing branch."""

    def test_names_the_missing_bare_repo(self, tmp_path, capsys):
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        err = capsys.readouterr().err
        assert str(config.root_dir / ".bare-git-repos" / "community.git") in err
        assert "ow update" in err

    def test_does_not_blame_the_branch(self, tmp_path, capsys):
        """The old path reported 'Branch <x> not found in local refs' instead."""
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        assert "not found in local refs" not in capsys.readouterr().err

    def test_labels_the_failure_as_resolve_not_fetch(self, tmp_path, capsys):
        """No fetch is attempted when resolution fails; 'git fetch ?' claimed otherwise."""
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        err = capsys.readouterr().err
        assert "resolve" in err
        assert "fetch ?" not in err

    def test_falls_back_to_the_declared_base_ref(self, tmp_path, capsys):
        config, ws, ws_dir = _workspace(tmp_path)

        tracks, _, _ = fetch_workspace_refs(ws, ws_dir, config)

        assert tracks["community"] == "origin/master"
