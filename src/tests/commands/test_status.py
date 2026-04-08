import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_status
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config


def write_ow_config(ws_dir: Path, templates: list[str], repos: dict[str, str], vars: dict | None = None) -> None:
    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        templates=templates,
        vars=vars or {},
    )
    write_workspace_config(ws_dir / ".ow" / "config", ws)


def _mock_parallel_exec(tasks):
    return {k: fn() for k, fn in tasks.items()}


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_cmd_status_drift_warns(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
        root_dir=tmp_path,
    )

    resolved_spec = BranchSpec("origin/master")
    fetch_return = ({"community": "origin/master"}, {}, {"community": resolved_spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.status.get_all_remote_refs", return_value={"origin/master"}),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_status_fetches_before_display(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
        root_dir=tmp_path,
    )

    fetch_called = [False]
    resolved_spec = BranchSpec("origin/master")

    def mock_fetch(*a, **kw):
        fetch_called[0] = True
        return ({"community": "origin/master"}, {}, {"community": resolved_spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.commands.status.fetch_workspace_refs", side_effect=mock_fetch),
        patch("ow.commands.status.get_all_remote_refs", return_value={"origin/master"}),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    assert fetch_called[0]
