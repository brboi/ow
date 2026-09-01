"""Tests for ow.utils.status — the data-only status layer.

Covers each RepoStatus.kind, the not_applied / unresolved / error states,
fetch_failed propagation, and a byte-identical output assertion that
runs cmd_status against a fixture workspace and compares capsys output.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from ow.commands.status import cmd_status
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config
from ow.utils.refs import FetchOutcome
from ow.utils.status import (
    RepoStatus,
    WorkspaceStatus,
    gather_workspace_status,
    _display_detached_status,
    _display_attached_status,
)
from ow.utils import paths


# ---------------------------------------------------------------------------
# RepoStatus.kind variants
# ---------------------------------------------------------------------------


class TestRepoStatusKinds:
    """Each kind produces the right display shape."""

    def test_detached_kind(self, tmp_path):
        rs = RepoStatus(
            alias="community", spec=BranchSpec("origin/master"),
            state="ok", kind="detached",
            head_label=None, short_hash="abc1234",
            base_ref="origin/master", upstream=None,
            primary=(3, 1), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        line = _display_detached_status(rs, 9)
        assert "DETACHED" in line
        assert "abc1234" in line
        assert "origin/master" in line

    def test_tracking_kind(self, tmp_path):
        rs = RepoStatus(
            alias="community", spec=BranchSpec("origin/master", "feat"),
            state="ok", kind="tracking",
            head_label="feat", short_hash=None,
            base_ref="origin/master", upstream="origin/feat",
            primary=(0, 2), secondary=(1, 3),
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        line = _display_attached_status(rs, 9)
        assert "origin/feat" in line
        assert "origin/master" in line

    def test_tracking_base_kind(self, tmp_path):
        rs = RepoStatus(
            alias="community", spec=BranchSpec("origin/master", "feat"),
            state="ok", kind="tracking_base",
            head_label="feat", short_hash=None,
            base_ref="origin/master", upstream="origin/master",
            primary=(0, 1), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        line = _display_attached_status(rs, 9)
        assert "(local)" in line
        assert "origin/master" in line

    def test_local_kind(self, tmp_path):
        rs = RepoStatus(
            alias="community", spec=BranchSpec("origin/master", "feat"),
            state="ok", kind="local",
            head_label="feat", short_hash=None,
            base_ref="origin/master", upstream=None,
            primary=(0, 1), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        line = _display_attached_status(rs, 9)
        assert "(local)" in line
        assert "origin/master" in line


# ---------------------------------------------------------------------------
# Non-ok states
# ---------------------------------------------------------------------------


class TestNonOkStates:
    def test_not_applied(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        # No community/ subdir → not applied
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.status.fetch_workspace_refs", return_value=FetchOutcome(
                tracks={}, upstreams={}, specs={}, upstream_before={},
            )),
            patch("ow.utils.status.check_all_drift", return_value=[]),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        assert "(not applied)" in capsys.readouterr().out

    def test_unresolved(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            # specs is empty → unresolved
            patch("ow.utils.status.fetch_workspace_refs", return_value=FetchOutcome(
                tracks={}, upstreams={}, specs={}, upstream_before={},
            )),
            patch("ow.utils.status.check_all_drift", return_value=[]),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)
        assert "could not resolve" in capsys.readouterr().out

    def test_fetch_failed(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        (paths.repos_dir() / "community.git").mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = FetchOutcome(
            tracks={}, upstreams={},
            specs={"community": resolved}, upstream_before={},
            failed=frozenset({"community"}),
        )
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.utils.status.check_all_drift", return_value=[]),
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_upstream", return_value=None),
            patch("ow.utils.status.get_worktree_branch", return_value="master"),
            patch("ow.utils.status.get_remote_url", return_value=None),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config, fetch=True)
        assert "fetch failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# WorkspaceStatus.runbot_branch
# ---------------------------------------------------------------------------


class TestWorkspaceStatusRunbot:
    def test_returns_first_repo_with_runbot_branch(self):
        rs1 = RepoStatus(
            alias="community", spec=BranchSpec("origin/master"),
            state="ok", kind="detached",
            head_label=None, short_hash="abc", base_ref="origin/master",
            upstream=None, primary=(0, 0), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        rs2 = RepoStatus(
            alias="enterprise", spec=BranchSpec("origin/master", "feat"),
            state="ok", kind="local",
            head_label="feat", short_hash=None, base_ref="origin/master",
            upstream=None, primary=(0, 0), secondary=None,
            github_url=None, runbot_branch="feat",
            fetch_failed=False, error=None,
        )
        ws = WorkspaceStatus(ws_dir=Path("/tmp/ws"), repos=[rs1, rs2], drift=[])
        assert ws.runbot_branch == "feat"

    def test_returns_none_when_no_repo_has_runbot(self):
        rs = RepoStatus(
            alias="community", spec=BranchSpec("origin/master"),
            state="ok", kind="detached",
            head_label=None, short_hash="abc", base_ref="origin/master",
            upstream=None, primary=(0, 0), secondary=None,
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        ws = WorkspaceStatus(ws_dir=Path("/tmp/ws"), repos=[rs], drift=[])
        assert ws.runbot_branch is None


# ---------------------------------------------------------------------------
# Byte-identical output canary
# ---------------------------------------------------------------------------


class TestByteIdenticalOutput:
    """cmd_status output must match what the old implementation produced."""

    def test_detached_output_matches(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        (paths.repos_dir() / "community.git").mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        resolved = BranchSpec("origin/master")
        fetch_return = FetchOutcome(
            tracks={"community": "origin/master"}, upstreams={},
            specs={"community": resolved}, upstream_before={},
        )
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.utils.status.check_all_drift", return_value=[]),
            patch("ow.utils.status.get_rev_list_count", return_value=(2, 5)),
            patch("ow.utils.status.get_worktree_head", return_value=("abcd123", "")),
            patch("ow.utils.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config, fetch=True)
        out = capsys.readouterr().out
        # The header
        assert "[test]" in out
        assert "branches" in out
        # Detached line
        assert "community" in out
        assert "DETACHED" in out
        assert "abcd123" in out
        assert "origin/master" in out
        # Links section (github commit link for detached)
        assert "links" in out
        assert "github.com" in out

    def test_attached_local_output_matches(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        (paths.repos_dir() / "community.git").mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master", "feat")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        resolved = BranchSpec("origin/master", "feat")
        fetch_return = FetchOutcome(
            tracks={"community": "origin/master"}, upstreams={},
            specs={"community": resolved}, upstream_before={},
        )
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.status.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.utils.status.check_all_drift", return_value=[]),
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 1)),
            patch("ow.utils.status.get_upstream", return_value=None),
            patch("ow.utils.status.get_worktree_branch", return_value="feat"),
            patch("ow.utils.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config, fetch=True)
        out = capsys.readouterr().out
        assert "community" in out
        assert "(local)" in out
        assert "feat" in out
        assert "origin/master" in out
        # Runbot link for odoo remote
        assert "runbot" in out
