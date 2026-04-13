from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config

from ow.commands.rebase import (
    RebasePlan,
    _analyze_repo_for_rebase,
    _display_rebase_summary,
    _do_rebase,
    _recover_with_cherry_pick,
    _report_conflict,
    cmd_rebase,
)


class TestReportConflict:
    def test_prints_instructions(self, capsys):
        _report_conflict("community", Path("/ws/community"), "origin/master")
        captured = capsys.readouterr()
        assert "CONFLICT" in captured.out
        assert "rebase --continue" in captured.out
        assert "rebase --abort" in captured.out


class TestDisplayRebaseSummary:
    def test_detached_plan(self, capsys):
        plans = [RebasePlan(
            alias="community", track_ref="origin/18.0", upstream=None,
            is_detached=True, local_commits=0, unpushed_commits=0,
            fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
            has_conflicts=False,
        )]
        _display_rebase_summary(plans)
        captured = capsys.readouterr()
        assert "community" in captured.out
        assert "origin/18.0" in captured.out

    def test_upstream_rewritten_no_fork(self, capsys):
        plans = [RebasePlan(
            alias="enterprise", track_ref="origin/master", upstream="origin/master",
            is_detached=False, local_commits=42, unpushed_commits=42,
            fork_point=None, commits_to_reapply=[], upstream_rewritten=True,
            has_conflicts=False,
        )]
        _display_rebase_summary(plans)
        captured = capsys.readouterr()
        output = captured.out.replace("\n", "")
        assert "rewritten, no fork-point" in output

    def test_upstream_rewritten_with_fork(self, capsys):
        plans = [RebasePlan(
            alias="community", track_ref="origin/master", upstream="origin/master",
            is_detached=False, local_commits=2, unpushed_commits=2,
            fork_point="abc123", commits_to_reapply=["abc123", "def456"],
            upstream_rewritten=True, has_conflicts=False,
        )]
        _display_rebase_summary(plans)
        captured = capsys.readouterr()
        output = captured.out.replace("\n", "")
        assert "rewritten, recoverable" in output

    def test_unpushed_commits_marker(self, capsys):
        plans = [RebasePlan(
            alias="enterprise", track_ref="origin/master", upstream="origin/master",
            is_detached=False, local_commits=1, unpushed_commits=3,
            fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
            has_conflicts=False,
        )]
        _display_rebase_summary(plans)
        captured = capsys.readouterr()
        assert "unpushed" in captured.out

    def test_conflict_in_progress(self, capsys):
        plans = [RebasePlan(
            alias="community", track_ref="origin/master", upstream="origin/master",
            is_detached=False, local_commits=0, unpushed_commits=0,
            fork_point="abc", commits_to_reapply=[], upstream_rewritten=False,
            has_conflicts=True,
        )]
        _display_rebase_summary(plans)
        captured = capsys.readouterr()
        assert "in progress" in captured.out

    def test_multiple_plans(self, capsys):
        plans = [
            RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=0, unpushed_commits=0,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=False,
            ),
            RebasePlan(
                alias="enterprise", track_ref="origin/master", upstream="origin/master",
                is_detached=True, local_commits=1, unpushed_commits=0,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=False,
            ),
        ]
        _display_rebase_summary(plans)
        captured = capsys.readouterr()
        assert "community" in captured.out
        assert "enterprise" in captured.out


class TestRebasePlan:
    def test_creation(self):
        plan = RebasePlan(
            alias="test", track_ref="origin/master", upstream="origin/master",
            is_detached=False, local_commits=1, unpushed_commits=2,
            fork_point="abc", commits_to_reapply=["abc"], upstream_rewritten=False,
            has_conflicts=False,
        )
        assert plan.alias == "test"
        assert plan.upstream_rewritten is False
        assert plan.commits_to_reapply == ["abc"]


class TestAnalyzeRepoForRebase:
    def test_detached_no_upstream(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        with patch("ow.commands.rebase.get_rev_list_count", return_value=(5, 0)):
            plan = _analyze_repo_for_rebase(worktree, "origin/18.0", None, "community", True)
        assert plan.alias == "community"
        assert plan.is_detached is True
        assert plan.has_conflicts is False
        assert plan.fork_point is None
        assert plan.local_commits == 5

    def test_attached_no_upstream(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        with patch("ow.commands.rebase.get_rev_list_count", return_value=(0, 0)):
            plan = _analyze_repo_for_rebase(worktree, "origin/master", None, "community", False)
        assert plan.upstream_rewritten is False
        assert plan.commits_to_reapply == []
        assert plan.unpushed_commits == 0

    def test_attached_with_upstream_no_fork(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        with (
            patch("ow.commands.rebase.get_rev_list_count", return_value=(3, 0)),
            patch("ow.commands.rebase.get_worktree_branch", return_value="my-branch"),
            patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None),
        ):
            plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)
        assert plan.upstream_rewritten is True
        assert plan.commits_to_reapply == []
        assert plan.unpushed_commits == 3

    def test_attached_with_upstream_and_fork_point(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        with (
            patch("ow.commands.rebase.get_rev_list_count", return_value=(2, 0)),
            patch("ow.commands.rebase.get_worktree_branch", return_value="my-branch"),
            patch("ow.commands.rebase.git_merge_base_fork_point", return_value="abc"),
            patch("ow.commands.rebase.git_rev_list", return_value=["abc", "def"]),
        ):
            plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)
        assert plan.fork_point == "abc"
        assert plan.commits_to_reapply == ["abc", "def"]
        assert plan.upstream_rewritten is False

    def test_conflict_detected_when_rebase_merge_exists(self, tmp_path):
        worktree = tmp_path / "community"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        (worktree / ".git" / "rebase-merge").mkdir()
        with (
            patch("ow.commands.rebase.get_rev_list_count", return_value=(0, 0)),
            patch("ow.commands.rebase.get_worktree_branch", return_value=None),
            patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None),
        ):
            plan = _analyze_repo_for_rebase(worktree, "origin/master", None, "community", False)
        assert plan.has_conflicts is True


class TestDoRebase:
    def test_returns_true_on_success(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with patch("ow.commands.rebase.git_rebase", return_value=MagicMock(returncode=0)):
            assert _do_rebase(worktree, "origin/master", "origin/feature") is True

    def test_returns_false_on_upstream_fail(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with patch("ow.commands.rebase.git_rebase") as mock_rb:
            mock_rb.return_value = MagicMock(returncode=1)
            assert _do_rebase(worktree, "origin/master", "origin/feature") is False

    def test_returns_false_on_track_fail(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        calls = []
        def mock_rb(*a, **kw):
            calls.append(a)
            if len(calls) == 1:
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)
        with patch("ow.commands.rebase.git_rebase", side_effect=mock_rb):
            assert _do_rebase(worktree, "origin/master", "origin/feature") is False


class TestRecoverWithCherryPick:
    def test_success_returns_none(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        commits = ["abc123", "def456"]
        with (
            patch("ow.commands.rebase.git_reset_hard"),
            patch("ow.commands.rebase.git_log_oneline", return_value="abc fix: x"),
            patch("ow.commands.rebase.git_cherry_pick", return_value=MagicMock(returncode=0)),
        ):
            result = _recover_with_cherry_pick(worktree, "origin/master", commits)
        assert result is None

    def test_conflict_returns_commit(self, tmp_path, capsys):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        commits = ["abc123", "fail_commit", "def456"]
        def mock_cp(*a, **kw):
            commit = a[1]
            if commit == "abc123":
                return MagicMock(returncode=0)
            return MagicMock(returncode=1)
        with (
            patch("ow.commands.rebase.git_reset_hard"),
            patch("ow.commands.rebase.git_log_oneline", return_value="abc fix"),
            patch("ow.commands.rebase.git_cherry_pick", side_effect=mock_cp),
        ):
            result = _recover_with_cherry_pick(worktree, "origin/master", commits)
        assert result == "fail_commit"


# ---------------------------------------------------------------------------
# cmd_rebase — execution paths
# ---------------------------------------------------------------------------

class TestCmdRebaseExtended:
    def test_cmd_rebase_aborted_by_user(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/master")
        fetch_return = ({"community": "origin/master"}, {}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value="n"),
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=1, unpushed_commits=0,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=False,
            )}
            cmd_rebase(config)
        captured = capsys.readouterr()
        assert "Aborted." in captured.out

    def test_cmd_rebase_detached_worktree(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/18.0")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/18.0")
        fetch_return = ({"community": "origin/18.0"}, {}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value=""),
            patch("ow.commands.rebase.git_switch") as mock_switch,
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/18.0", upstream=None,
                is_detached=True, local_commits=0, unpushed_commits=0,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=False,
            )}
            cmd_rebase(config)
        mock_switch.assert_called_once()

    def test_cmd_rebase_conflict_skip(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/master")
        fetch_return = ({"community": "origin/master"}, {}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value=""),
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=1, unpushed_commits=0,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=True,
            )}
            cmd_rebase(config)
        captured = capsys.readouterr()
        assert "rebase already in progress" in captured.out

    def test_cmd_rebase_no_fork_point_skip(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/master")
        fetch_return = ({"community": "origin/master"}, {}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value=""),
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=2, unpushed_commits=2,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=True,
                has_conflicts=False,
            )}
            cmd_rebase(config)
        captured = capsys.readouterr()
        output = captured.out.replace("\n", "")
        assert "no fork-point" in output
        assert "Manual recovery" in output


class TestCmdRebaseExecution:

    def test_cmd_rebase_plain_attached_success(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master", "my-feature")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/master", "my-feature")
        fetch_return = ({"community": "origin/my-feature"}, {"community": "origin/master"}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value=""),
            patch("ow.commands.rebase.get_worktree_branch", return_value="my-feature"),
            patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None),
            patch("ow.commands.rebase.get_rev_list_count", return_value=(2, 0)),
            patch("ow.commands.rebase.git_rebase", return_value=MagicMock(returncode=0)),
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=2, unpushed_commits=0,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=False,
            )}
            cmd_rebase(config)
        captured = capsys.readouterr()
        assert "Done." in captured.out or "community" in captured.out

    def test_cmd_rebase_recovery_success(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master", "my-feature")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/master", "my-feature")
        fetch_return = ({"community": "origin/my-feature"}, {"community": "origin/master"}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value=""),
            patch("ow.commands.rebase.git_reset_hard"),
            patch("ow.commands.rebase.git_log_oneline", return_value="abc fix"),
            patch("ow.commands.rebase.git_cherry_pick", return_value=MagicMock(returncode=0)),
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=0, unpushed_commits=2,
                fork_point="abc", commits_to_reapply=["abc123"], upstream_rewritten=True,
                has_conflicts=False,
            )}
            cmd_rebase(config)
        captured = capsys.readouterr()
        assert "Done (recovered)" in captured.out

    def test_cmd_rebase_unpushed_warning(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        (ws_dir / "community").mkdir()
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master", "my-feature")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        resolved = BranchSpec("origin/master", "my-feature")
        fetch_return = ({"community": "origin/my-feature"}, {"community": "origin/master"}, {"community": resolved})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.get_worktree_branch", return_value=None),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
            patch("ow.commands.rebase.parallel_per_repo") as mock_analyze,
            patch("builtins.input", return_value="n"),
        ):
            mock_analyze.return_value = {"community": RebasePlan(
                alias="community", track_ref="origin/master", upstream="origin/master",
                is_detached=False, local_commits=0, unpushed_commits=5,
                fork_point=None, commits_to_reapply=[], upstream_rewritten=False,
                has_conflicts=False,
            )}
            cmd_rebase(config)
        captured = capsys.readouterr()
        assert "unpushed commits" in captured.out

    def test_cmd_rebase_no_worktrees_returns(self, tmp_path, capsys, config_with_remotes):
        ws_dir = tmp_path / "workspaces" / "test"
        ws_dir.mkdir(parents=True)
        # No worktree dirs
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")},
            templates=["common"],
        )
        write_workspace_config(ws_dir / ".ow" / "config", ws)
        config = config_with_remotes
        fetch_return = ({"community": "origin/master"}, {}, {"community": BranchSpec("origin/master")})
        with (
            patch.dict("os.environ", {"OW_WORKSPACE": str(ws_dir)}),
            patch("ow.utils.drift.parallel_per_repo", side_effect=lambda t: {k: fn() for k, fn in t.items()}),
            patch("ow.utils.drift.get_worktree_branch", side_effect=FileNotFoundError),
            patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        ):
            cmd_rebase(config)
        captured = capsys.readouterr()
        # Should complete without error
        assert captured.err == "" or "Error" not in captured.err
