import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_status
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config
from ow.utils.refs import FetchOutcome
from ow.utils import paths


def write_ow_config(ws_dir: Path, templates: list[str], repos: dict[str, str], vars: dict | None = None) -> None:
    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        templates=templates,
        vars=vars or {},
    )
    write_workspace_config(ws_dir / ".ow" / "config.toml", ws)


def _mock_parallel_exec(tasks):
    return {k: fn() for k, fn in tasks.items()}


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

def test_cmd_status_drift_warns(tmp_path, capsys, xdg):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
    )

    resolved_spec = BranchSpec("origin/master")
    fetch_return = FetchOutcome(
        tracks={"community": "origin/master"}, upstreams={},
        specs={"community": resolved_spec}, upstream_before={},
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_status_fetches_before_display(tmp_path, xdg):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
    )

    fetch_called = [False]
    resolved_spec = BranchSpec("origin/master")

    def mock_fetch(*a, **kw):
        fetch_called[0] = True
        return FetchOutcome(
            tracks={"community": "origin/master"}, upstreams={},
            specs={"community": resolved_spec}, upstream_before={},
        )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.commands.status.fetch_workspace_refs", side_effect=mock_fetch),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config, fetch=True)

    assert fetch_called[0]


def test_cmd_status_marks_fetch_failure(tmp_path, capsys, xdg):
    """When fetch_workspace_refs reports a failed alias, status shows a marker."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
    )

    resolved_spec = BranchSpec("origin/master")
    fetch_return = FetchOutcome(
        tracks={"community": "origin/master"}, upstreams={},
        specs={"community": resolved_spec}, upstream_before={},
        failed=frozenset({"community"}),
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.commands.status.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.status._gather_repo_status", return_value=MagicMock(
            status_line="        community: origin/master", first_attached_branch=None, github_link=None,
        )),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config, fetch=True)

    captured = capsys.readouterr()
    assert "fetch failed" in captured.out


def test_status_offline_does_not_fetch(tmp_path, xdg, monkeypatch):
    """When fetch=False (default), fetch_workspace_refs is NOT called."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = Config(vars={}, remotes={})

    fetch_called = []
    monkeypatch.setattr(
        "ow.commands.status.fetch_workspace_refs",
        lambda *a, **kw: fetch_called.append(True),
    )
    monkeypatch.setattr("ow.commands.status.warn_if_drifted", lambda *a, **kw: None)
    monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))

    cmd_status(config)  # no fetch=True
    assert not fetch_called, "status fetched without --fetch"


def test_status_fetch_calls_fetch(tmp_path, xdg, monkeypatch):
    """When fetch=True, fetch_workspace_refs IS called."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = Config(vars={}, remotes={})

    fetch_called = []
    def mock_fetch(*a, **kw):
        fetch_called.append(True)
        return FetchOutcome(
            tracks={}, upstreams={}, specs={}, upstream_before={},
        )
    monkeypatch.setattr(
        "ow.commands.status.fetch_workspace_refs",
        mock_fetch,
    )
    monkeypatch.setattr("ow.commands.status.warn_if_drifted", lambda *a, **kw: None)
    monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))

    cmd_status(config, fetch=True)
    assert fetch_called
