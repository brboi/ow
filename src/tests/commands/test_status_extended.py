import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands.status import cmd_status
from ow.utils.status import (
    RepoStatus,
    WorkspaceStatus,
    _gather_one_repo,
    _display_detached_status,
    _display_attached_status,
    github_url_from_remote,
)
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config
from ow.utils.config import RemoteConfig
from ow.utils.refs import FetchOutcome


class TestGithubUrlFromRemote:
    def test_ssh_url(self):
        result = github_url_from_remote("git@github.com:odoo/odoo.git")
        assert result == "https://github.com/odoo/odoo"

    def test_ssh_url_no_dotgit(self):
        result = github_url_from_remote("git@github.com:odoo/odoo")
        assert result == "https://github.com/odoo/odoo"

    def test_https_url(self):
        result = github_url_from_remote("https://github.com/odoo/odoo.git")
        assert result == "https://github.com/odoo/odoo"

    def test_https_url_no_dotgit(self):
        result = github_url_from_remote("https://github.com/odoo/odoo")
        assert result == "https://github.com/odoo/odoo"

    def test_gitlab_ssh_url_returns_none(self):
        result = github_url_from_remote("git@gitlab.com:odoo/odoo.git")
        assert result is None

    def test_unknown_format_returns_none(self):
        result = github_url_from_remote("https://gitlab.company.com/odoo/odoo.git")
        assert result is None

    def test_empty_string_returns_none(self):
        result = github_url_from_remote("")
        assert result is None


def _make_detached_status(**overrides):
    defaults = dict(
        alias="community", spec=BranchSpec("origin/master"),
        state="ok", kind="detached",
        head_label=None, short_hash="abcd123",
        base_ref="origin/master", upstream=None,
        primary=(5, 2), secondary=None,
        github_url=None, runbot_branch=None,
        fetch_failed=False, error=None,
    )
    defaults.update(overrides)
    return RepoStatus(**defaults)


def _make_attached_status(**overrides):
    defaults = dict(
        alias="community", spec=BranchSpec("origin/master", "my-feature"),
        state="ok", kind="local",
        head_label="my-feature", short_hash=None,
        base_ref="origin/master", upstream=None,
        primary=(1, 0), secondary=None,
        github_url=None, runbot_branch=None,
        fetch_failed=False, error=None,
    )
    defaults.update(overrides)
    return RepoStatus(**defaults)


class TestDisplayDetachedStatus:
    def test_detached_status_line(self, tmp_path):
        rs = _make_detached_status()
        result = _display_detached_status(rs, 9)

        assert "community" in result
        assert "DETACHED" in result
        assert "abcd123" in result
        assert "origin/master" in result


class TestDisplayAttachedStatus:
    def test_attached_with_upstream_not_base(self, tmp_path):
        """When upstream exists and differs from base, show upstream + base."""
        rs = RepoStatus(
            alias="community", spec=BranchSpec("origin/master", "my-feature"),
            state="ok", kind="tracking",
            head_label="my-feature", short_hash=None,
            base_ref="origin/master", upstream="origin/my-feature",
            primary=(1, 0), secondary=(2, 0),
            github_url=None, runbot_branch=None,
            fetch_failed=False, error=None,
        )
        result = _display_attached_status(rs, 9)
        assert "origin/my-feature" in result
        assert "origin/master" in result

    def test_attached_no_remote_ref_no_upstream(self, tmp_path):
        rs = _make_attached_status(kind="local", upstream=None)
        result = _display_attached_status(rs, 9)

        assert "my-feature" in result
        assert "(local)" in result
        assert "origin/master" in result


class TestGatherRepoStatus:
    def test_gather_detached_with_github_link(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master")

        with (
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_worktree_head", return_value=("abc123", "")),
            patch("ow.utils.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
        ):
            result = _gather_one_repo(
                "community", spec, spec, worktree, bare_repo, False,
            )

        assert isinstance(result, RepoStatus)
        assert result.kind == "detached"
        assert result.runbot_branch is None
        assert result.github_url is not None
        assert "github.com" in result.github_url

    def test_gather_attached_branch(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master", "feature")
        resolved = BranchSpec("origin/master", "feature")

        with (
            patch("ow.utils.status.get_upstream", return_value=None),
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
            patch("ow.utils.status.get_worktree_branch", return_value="feature"),
        ):
            result = _gather_one_repo(
                "community", spec, resolved, worktree, bare_repo, False,
            )

        assert isinstance(result, RepoStatus)
        assert result.runbot_branch == "feature"
        assert result.github_url is not None
        assert "tree/feature" in result.github_url

    def test_gather_non_github_remote(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master")

        with (
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_worktree_head", return_value=("abc", "")),
            patch("ow.utils.status.get_remote_url", return_value="https://gitlab.server.com/odoo.git"),
        ):
            result = _gather_one_repo(
                "community", spec, spec, worktree, bare_repo, False,
            )

        assert result.github_url is None

    def test_gather_no_remote_url(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master")

        with (
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_worktree_head", return_value=("abc", "")),
            patch("ow.utils.status.get_remote_url", return_value=None),
        ):
            result = _gather_one_repo(
                "community", spec, spec, worktree, bare_repo, False,
            )

        assert result.github_url is None


class TestCmdStatusExtended:
    def test_cmd_status_no_worktrees(self, tmp_path, capsys, config):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)

        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.status.fetch_workspace_refs", return_value=FetchOutcome(
                tracks={"community": "origin/master"}, upstreams={}, specs={}, upstream_before={},
            )),
            patch("ow.utils.status.check_all_drift", return_value=[]),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)

        captured = capsys.readouterr()
        assert "test" in captured.out
        assert "(not applied)" in captured.out


def _mock_parallel_exec(tasks):
    return {k: fn() for k, fn in tasks.items()}


class TestAttachedStatusLabelsTheCheckedOutBranch:
    """D4 — the counts come from HEAD, so the label must too."""

    def test_a_drifted_worktree_is_not_labelled_with_the_configured_branch(self, tmp_path):
        rs = _make_attached_status(
            kind="local", upstream=None,
            head_label="sidetrack",
            spec=BranchSpec("origin/master", "feat"),
        )
        result = _display_attached_status(rs, 9)

        assert "sidetrack" in result
        assert "feat" not in result.replace("sidetrack", "")

    def test_the_upstream_equals_base_line_names_the_checked_out_branch(self, tmp_path):
        rs = _make_attached_status(
            kind="tracking_base", upstream="origin/master",
            head_label="sidetrack",
            spec=BranchSpec("origin/master", "feat"),
        )
        result = _display_attached_status(rs, 9)

        assert "sidetrack" in result
        assert "feat" not in result.replace("sidetrack", "")

    def test_an_aligned_worktree_reads_as_before(self, tmp_path):
        rs = _make_attached_status(
            kind="local", upstream=None,
            head_label="feat",
            spec=BranchSpec("origin/master", "feat"),
        )
        result = _display_attached_status(rs, 9)

        assert "feat" in result

    def test_a_detached_head_is_not_labelled_with_a_branch_at_all(self, tmp_path):
        """Config says attached, HEAD is not. Naming any branch would lie."""
        rs = _make_attached_status(
            kind="local", upstream=None,
            head_label=None,
            spec=BranchSpec("origin/master", "feat"),
        )
        result = _display_attached_status(rs, 9)

        assert "DETACHED" in result
        assert "feat" not in result


class TestRunbotLinkOnlyForOdoo:
    """D5 — runbot only knows bundles for the odoo organisation's repos."""

    def _gather(self, tmp_path, remote_url):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare = tmp_path / "community.git"
        spec = resolved = BranchSpec("origin/master", "feat")
        with (
            patch("ow.utils.status.get_upstream", return_value=None),
            patch("ow.utils.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.utils.status.get_worktree_branch", return_value="feat"),
            patch("ow.utils.status.get_remote_url", return_value=remote_url),
        ):
            return _gather_one_repo("community", spec, resolved, worktree, bare, False)

    def test_a_local_file_remote_gets_no_runbot_bundle(self, tmp_path):
        assert self._gather(tmp_path, "file:///srv/mirrors/odoo.git").runbot_branch is None

    def test_a_third_party_github_remote_gets_no_runbot_bundle(self, tmp_path):
        assert self._gather(tmp_path, "git@github.com:acme/odoo.git").runbot_branch is None

    def test_a_missing_remote_url_gets_no_runbot_bundle(self, tmp_path):
        assert self._gather(tmp_path, None).runbot_branch is None

    def test_an_odoo_remote_still_gets_its_bundle(self, tmp_path):
        assert self._gather(tmp_path, "git@github.com:odoo/odoo.git").runbot_branch == "feat"

    def test_an_odoo_https_remote_still_gets_its_bundle(self, tmp_path):
        assert self._gather(
            tmp_path, "https://github.com/odoo/enterprise",
        ).runbot_branch == "feat"
