import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.commands.status import cmd_status
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config
from ow.utils.options import OdooOverrides
from ow.utils.refs import FetchOutcome
from ow.utils import paths


def write_ow_config(
    ws_dir: Path, repos: dict[str, str], odoo: OdooOverrides | None = None
) -> None:
    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        odoo=odoo if odoo is not None else OdooOverrides(),
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
    write_ow_config(ws_dir, {"community": "master..my-feature"})
    config = Config(remotes={})

    resolved_spec = BranchSpec("origin/master")
    fetch_return = FetchOutcome(
        tracks={"community": "origin/master"}, upstreams={},
        specs={"community": resolved_spec}, upstream_before={},
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="wrong-branch"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.status.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.utils.status._gather_one_repo", return_value=MagicMock(
            alias="community", state="ok", kind="tracking_base",
            head_label="my-feature", short_hash=None,
            base_ref="origin/master", upstream="origin/my-feature",
            primary=(0, 0), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
            spec=BranchSpec("origin/master", "my-feature"),
        )),
        patch("ow.utils.status.check_all_drift", return_value=[]),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_status_fetches_before_display(tmp_path, xdg):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, {"community": "master"})
    config = Config(remotes={})

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
        patch("ow.utils.status.fetch_workspace_refs", side_effect=mock_fetch),
        patch("ow.utils.status._gather_one_repo", return_value=MagicMock(
            alias="community", state="ok", kind="tracking_base",
            head_label="master", short_hash=None,
            base_ref="origin/master", upstream=None,
            primary=(0, 0), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
            spec=BranchSpec("origin/master"),
        )),
        patch("ow.utils.status.check_all_drift", return_value=[]),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_status(config, fetch=True)

    assert fetch_called[0]


def test_cmd_status_marks_fetch_failure(tmp_path, capsys, xdg):
    """When fetch_workspace_refs reports a failed alias, status shows a marker."""
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (paths.repos_dir() / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, {"community": "master"})
    config = Config(remotes={})

    resolved_spec = BranchSpec("origin/master")
    fetch_return = FetchOutcome(
        tracks={"community": "origin/master"}, upstreams={},
        specs={"community": resolved_spec}, upstream_before={},
        failed=frozenset({"community"}),
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.status.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.utils.status._gather_one_repo", return_value=MagicMock(
            alias="community", state="ok", kind="tracking_base",
            head_label="master", short_hash=None,
            base_ref="origin/master", upstream=None,
            primary=(0, 0), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=True, error=None,
            spec=BranchSpec("origin/master"),
        )),
        patch("ow.utils.status.check_all_drift", return_value=[]),
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
    write_ow_config(ws_dir, {"community": "master"})
    config = Config(remotes={})

    fetch_called = []
    monkeypatch.setattr(
        "ow.utils.status.fetch_workspace_refs",
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
    write_ow_config(ws_dir, {"community": "master"})
    config = Config(remotes={})

    fetch_called = []
    def mock_fetch(*a, **kw):
        fetch_called.append(True)
        return FetchOutcome(
            tracks={}, upstreams={}, specs={}, upstream_before={},
        )
    monkeypatch.setattr(
        "ow.utils.status.fetch_workspace_refs",
        mock_fetch,
    )
    monkeypatch.setattr("ow.commands.status.warn_if_drifted", lambda *a, **kw: None)
    monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))

    cmd_status(config, fetch=True)
    assert fetch_called


def test_cmd_status_reports_a_pending_migration(tmp_path, capsys, xdg):
    """`ow status` shows the condition and names the command that resolves it.

    Reporting is not migrating: status must not write a byte of the file it
    is describing, and the warning is the only thing that explains why typed
    options are not in play for this workspace yet."""
    ws_dir = tmp_path / "workspaces" / "legacy"
    (ws_dir / ".ow").mkdir(parents=True)
    (ws_dir / ".ow" / "config.toml").write_text('[repos]\ncommunity = "master"\n')

    cmd_status(Config(remotes={}), workspace=str(ws_dir))

    err = capsys.readouterr().err
    assert "Pending migration" in err
    assert "ow render" in err
    assert "version" not in (ws_dir / ".ow" / "config.toml").read_text()


def test_cmd_status_says_nothing_about_migration_for_schema_2(tmp_path, capsys, xdg):
    ws_dir = tmp_path / "workspaces" / "current"
    write_ow_config(ws_dir, {"community": "master"})

    cmd_status(Config(remotes={}), workspace=str(ws_dir))

    assert "Pending migration" not in capsys.readouterr().err


def test_cmd_status_reports_an_unreadable_schema_1_lock_without_a_traceback(tmp_path, capsys, xdg):
    """The lock blocker reaches the status diagnostics as text, not as a raise."""
    ws_dir = tmp_path / "workspaces" / "legacy"
    (ws_dir / "community").mkdir(parents=True)
    (ws_dir / ".ow").mkdir()
    (ws_dir / ".ow" / "config.toml").write_text(
        'version = 1\n\n[repos]\ncommunity = "master..feat"\n'
    )
    lock = ws_dir / ".ow" / "rendered.lock.toml"
    lock.write_bytes(b"")
    lock.chmod(0o000)
    try:
        cmd_status(Config(remotes={}), workspace=str(ws_dir))
    finally:
        lock.chmod(0o600)

    captured = capsys.readouterr()
    assert str(lock) in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
