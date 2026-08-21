import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands.status import (
    _StatusResult,
    _display_attached_status,
    _display_detached_status,
    _gather_repo_status,
    _github_url_from_remote,
    cmd_status,
)
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config
from ow.utils.config import RemoteConfig
from ow.utils.refs import FetchOutcome


class TestGithubUrlFromRemote:
    def test_ssh_url(self):
        result = _github_url_from_remote("git@github.com:odoo/odoo.git")
        assert result == "https://github.com/odoo/odoo"

    def test_ssh_url_no_dotgit(self):
        result = _github_url_from_remote("git@github.com:odoo/odoo")
        assert result == "https://github.com/odoo/odoo"

    def test_https_url(self):
        result = _github_url_from_remote("https://github.com/odoo/odoo.git")
        assert result == "https://github.com/odoo/odoo"

    def test_https_url_no_dotgit(self):
        result = _github_url_from_remote("https://github.com/odoo/odoo")
        assert result == "https://github.com/odoo/odoo"

    def test_gitlab_ssh_url_returns_none(self):
        result = _github_url_from_remote("git@gitlab.com:odoo/odoo.git")
        assert result is None

    def test_unknown_format_returns_none(self):
        result = _github_url_from_remote("https://gitlab.company.com/odoo/odoo.git")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _github_url_from_remote("")
        assert result is None


class TestDisplayDetachedStatus:
    def test_detached_status_line(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = BranchSpec("origin/master")

        with (
            patch("ow.commands.status.get_rev_list_count", return_value=(2, 5)),
            patch("ow.commands.status.get_worktree_head", return_value=("abcd123", "")),
        ):
            result = _display_detached_status("community", spec, spec, worktree, 9)

        assert "community" in result
        assert "DETACHED" in result
        assert "abcd123" in result
        assert "origin/master" in result


class TestDisplayAttachedStatus:
    def test_attached_with_upstream_not_base(self, tmp_path):
        """When no remote ref but upstream exists and differs from base, show upstream + base."""
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = BranchSpec("origin/master", "my-feature")
        resolved = BranchSpec("origin/master", "my-feature")
        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value="origin/my-feature"),
            patch("ow.commands.status.get_rev_list_count") as mock_count,
        ):
            mock_count.side_effect = [(0, 1), (0, 2)]
            result = _display_attached_status(
                "community", spec, resolved, worktree, 9
            )
        assert "origin/my-feature" in result
        assert "origin/master" in result
        assert mock_count.call_count == 2

    def test_attached_no_remote_ref_no_upstream(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = BranchSpec("origin/master", "my-feature")
        resolved = BranchSpec("origin/master", "my-feature")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 1)),
            patch("ow.commands.status.get_worktree_branch", return_value="my-feature"),
        ):
            result = _display_attached_status(
                "community", spec, resolved, worktree, 9
            )

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
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_worktree_head", return_value=("abc123", "")),
            patch("ow.commands.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
        ):
            result = _gather_repo_status(
                "community", spec, spec, worktree, bare_repo, 9, set()
            )

        assert isinstance(result, _StatusResult)
        assert "DETACHED" in result.status_line
        assert result.first_attached_branch is None
        assert result.github_link is not None
        assert result.github_link[0] == "community"
        assert "github.com" in result.github_link[1]

    def test_gather_attached_branch(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master", "feature")
        resolved = BranchSpec("origin/master", "feature")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_remote_url", return_value="git@github.com:odoo/odoo.git"),
        ):
            result = _gather_repo_status(
                "community", spec, resolved, worktree, bare_repo, 9, set()
            )

        assert isinstance(result, _StatusResult)
        assert result.first_attached_branch == "feature"
        assert result.github_link is not None
        assert "tree/feature" in result.github_link[1]

    def test_gather_non_github_remote(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master")

        with (
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_worktree_head", return_value=("abc", "")),
            patch("ow.commands.status.get_remote_url", return_value="https://gitlab.server.com/odoo.git"),
        ):
            result = _gather_repo_status(
                "community", spec, spec, worktree, bare_repo, 9, set()
            )

        assert result.github_link is None

    def test_gather_no_remote_url(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare_repo = tmp_path / "community.git"
        bare_repo.mkdir()
        spec = BranchSpec("origin/master")

        with (
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_worktree_head", return_value=("abc", "")),
            patch("ow.commands.status.get_remote_url", return_value=None),
        ):
            result = _gather_repo_status(
                "community", spec, spec, worktree, bare_repo, 9, set()
            )

        assert result.github_link is None


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
            patch("ow.commands.status.fetch_workspace_refs", return_value=FetchOutcome(
                tracks={"community": "origin/master"}, upstreams={}, specs={}, upstream_before={},
            )),
            patch("ow.commands.status.warn_if_drifted"),
        ):
            cmd_status(config)

        captured = capsys.readouterr()
        assert "test" in captured.out
        assert "(not applied)" in captured.out


def _mock_parallel_exec(tasks):
    return {k: fn() for k, fn in tasks.items()}


class TestAttachedStatusLabelsTheCheckedOutBranch:
    """D4 — the counts come from HEAD, so the label must too.

    Under drift the line read `community: feat (local) (origin/master ↓0 ↑1)`
    while HEAD was on `sidetrack`: a commit that lives on sidetrack was
    attributed to feat. The drift warning above is the only thing that saved
    the user, and it scrolls off.
    """

    def test_a_drifted_worktree_is_not_labelled_with_the_configured_branch(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = resolved = BranchSpec("origin/master", "feat")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(1, 0)),
            patch("ow.commands.status.get_worktree_branch", return_value="sidetrack"),
        ):
            result = _display_attached_status("community", spec, resolved, worktree, 9)

        assert "sidetrack" in result
        assert "feat" not in result.replace("sidetrack", "")

    def test_the_upstream_equals_base_line_names_the_checked_out_branch(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = resolved = BranchSpec("origin/master", "feat")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value="origin/master"),
            patch("ow.commands.status.get_rev_list_count", return_value=(1, 0)),
            patch("ow.commands.status.get_worktree_branch", return_value="sidetrack"),
        ):
            result = _display_attached_status("community", spec, resolved, worktree, 9)

        assert "sidetrack" in result
        assert "feat" not in result.replace("sidetrack", "")

    def test_an_aligned_worktree_reads_as_before(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = resolved = BranchSpec("origin/master", "feat")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(1, 0)),
            patch("ow.commands.status.get_worktree_branch", return_value="feat"),
        ):
            result = _display_attached_status("community", spec, resolved, worktree, 9)

        assert "feat" in result

    def test_a_detached_head_is_not_labelled_with_a_branch_at_all(self, tmp_path):
        """Config says attached, HEAD is not. Naming any branch would lie."""
        worktree = tmp_path / "community"
        worktree.mkdir()
        spec = resolved = BranchSpec("origin/master", "feat")

        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(1, 0)),
            patch("ow.commands.status.get_worktree_branch", return_value=None),
        ):
            result = _display_attached_status("community", spec, resolved, worktree, 9)

        assert "DETACHED" in result
        assert "feat" not in result


class TestRunbotLinkOnlyForOdoo:
    """D5 — runbot only knows bundles for the odoo organisation's repos.

    A workspace on a local `file://` remote still got a
    `runbot: https://runbot.odoo.com/runbot/bundle/<branch>` line.
    """

    def _gather(self, tmp_path, remote_url):
        worktree = tmp_path / "community"
        worktree.mkdir()
        bare = tmp_path / "community.git"
        spec = resolved = BranchSpec("origin/master", "feat")
        with (
            patch("ow.commands.status.get_remote_ref_for_branch", return_value=None),
            patch("ow.commands.status.get_upstream", return_value=None),
            patch("ow.commands.status.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.status.get_worktree_branch", return_value="feat"),
            patch("ow.commands.status.get_remote_url", return_value=remote_url),
        ):
            return _gather_repo_status("community", spec, resolved, worktree, bare, 9, set())

    def test_a_local_file_remote_gets_no_runbot_bundle(self, tmp_path):
        assert self._gather(tmp_path, "file:///srv/mirrors/odoo.git").first_attached_branch is None

    def test_a_third_party_github_remote_gets_no_runbot_bundle(self, tmp_path):
        assert self._gather(tmp_path, "git@github.com:acme/odoo.git").first_attached_branch is None

    def test_a_missing_remote_url_gets_no_runbot_bundle(self, tmp_path):
        assert self._gather(tmp_path, None).first_attached_branch is None

    def test_an_odoo_remote_still_gets_its_bundle(self, tmp_path):
        assert self._gather(tmp_path, "git@github.com:odoo/odoo.git").first_attached_branch == "feat"

    def test_an_odoo_https_remote_still_gets_its_bundle(self, tmp_path):
        assert self._gather(
            tmp_path, "https://github.com/odoo/enterprise",
        ).first_attached_branch == "feat"
