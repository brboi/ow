import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ow.commands import cmd_rebase
from ow.commands.rebase import _analyze_repo_for_rebase, _recover_with_cherry_pick
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, parse_branch_spec, write_workspace_config


def write_ow_config(ws_dir: Path, templates: list[str], repos: dict[str, str], vars: dict | None = None) -> None:
    ws = WorkspaceConfig(
        repos={alias: parse_branch_spec(spec) for alias, spec in repos.items()},
        templates=templates,
        vars=vars or {},
    )
    write_workspace_config(ws_dir / ".ow" / "config", ws)


def _make_subprocess_mock(
    *,
    rebase_fail_on: list[str] | None = None,
    track_calls: dict[str, list] | None = None,
) -> Any:
    failed_rebases: set[str] = set()

    def side_effect(args, **kwargs):
        mock = MagicMock(returncode=0)
        mock.stdout = "0\t0\n"
        if track_calls is not None:
            if "rebase" in args and "rebase" in track_calls:
                track_calls["rebase"].append(args[-1])
            if "switch" in args and "switch" in track_calls:
                track_calls["switch"].append(list(args))
        if rebase_fail_on is not None and "rebase" in args:
            worktree = args[2] if len(args) > 2 else None
            if worktree in rebase_fail_on and worktree not in failed_rebases:
                failed_rebases.add(worktree)
                mock.returncode = 1
        return mock

    return side_effect


def _mock_parallel_exec(tasks):
    return {k: fn() for k, fn in tasks.items()}


# ---------------------------------------------------------------------------
# cmd_rebase
# ---------------------------------------------------------------------------

def test_cmd_rebase_drift_warns(tmp_path, capsys):
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
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=_make_subprocess_mock()),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    captured = capsys.readouterr()
    assert "Warning" in captured.err


def test_cmd_rebase_detached_switches(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
        root_dir=tmp_path,
    )

    switch_calls: list = []
    resolved_spec = BranchSpec("origin/master")
    fetch_return = ({"community": "origin/master"}, {}, {"community": resolved_spec})
    mock_sub = _make_subprocess_mock(track_calls={"switch": switch_calls})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value=None),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=mock_sub),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert any("--detach" in c for c in switch_calls)


def test_cmd_rebase_two_step_rebase(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
        root_dir=tmp_path,
    )

    rebase_targets: list = []
    track_run = _make_subprocess_mock(track_calls={"rebase": rebase_targets})

    def mock_spec(bare_repo, spec, remotes):
        if spec.local_branch == "my-feature":
            return BranchSpec("dev/my-feature", "my-feature")
        return BranchSpec("origin/master")

    fetch_return = (
        {"community": "dev/my-feature"},
        {"community": "origin/master"},
        {"community": BranchSpec("dev/my-feature", "my-feature")},
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.resolve_spec", side_effect=mock_spec),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert rebase_targets == ["dev/my-feature", "origin/master"]


def test_cmd_rebase_conflict_reports_and_continues(tmp_path, capsys):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (ws_dir / "enterprise").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "enterprise.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {
        "community": "master..my-feature",
        "enterprise": "master..my-feature",
    })
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
        root_dir=tmp_path,
    )

    community_path = str(ws_dir / "community")
    track_run = _make_subprocess_mock(rebase_fail_on=[community_path])

    def mock_spec(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    spec = BranchSpec("origin/master", "my-feature")
    fetch_return = (
        {"community": "origin/master", "enterprise": "origin/master"},
        {"community": "origin/master", "enterprise": "origin/master"},
        {"community": spec, "enterprise": spec},
    )

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.resolve_spec", side_effect=mock_spec),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        with pytest.raises(SystemExit):
            cmd_rebase(config)

    captured = capsys.readouterr()
    assert "CONFLICT" in captured.out


def test_cmd_rebase_no_upstream_when_not_pushed(tmp_path):
    ws_dir = tmp_path / "workspaces" / "test"
    (ws_dir / "community").mkdir(parents=True)
    (tmp_path / ".bare-git-repos" / "community.git").mkdir(parents=True)
    write_ow_config(ws_dir, ["common"], {"community": "master..my-feature"})
    config = Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={},
        root_dir=tmp_path,
    )

    rebase_targets: list = []
    track_run = _make_subprocess_mock(track_calls={"rebase": rebase_targets})

    def mock_spec(bare_repo, spec, remotes):
        return BranchSpec("origin/master", spec.local_branch)

    spec = BranchSpec("origin/master", "my-feature")
    fetch_return = ({"community": "origin/master"}, {}, {"community": spec})

    with (
        patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"),
        patch("ow.utils.drift.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.refs.fetch_workspace_refs", return_value=fetch_return),
        patch("ow.commands.rebase.resolve_spec", side_effect=mock_spec),
        patch("ow.commands.rebase.parallel_per_repo", side_effect=_mock_parallel_exec),
        patch("ow.utils.git.subprocess.run", side_effect=track_run),
        patch("builtins.input", return_value=""),
        patch.dict(os.environ, {"OW_WORKSPACE": str(ws_dir)}),
    ):
        cmd_rebase(config)

    assert rebase_targets == ["origin/master"]


# ---------------------------------------------------------------------------
# _recover_with_cherry_pick
# ---------------------------------------------------------------------------

def test_recover_with_cherry_pick_success_returns_none(tmp_path):
    """All cherry-picks succeed -> returns None."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    commits = ["aaa111", "bbb222", "ccc333"]

    mock_reset = MagicMock()
    mock_cp = MagicMock()
    mock_cp.return_value = MagicMock(returncode=0)
    mock_log = MagicMock(return_value="hash some message")

    with patch("ow.commands.rebase.git_reset_hard", mock_reset), \
         patch("ow.commands.rebase.git_cherry_pick", mock_cp), \
         patch("ow.commands.rebase.git_log_oneline", mock_log):
        result = _recover_with_cherry_pick(worktree, "origin/master", commits)

    assert result is None
    mock_reset.assert_called_once_with(worktree, "origin/master")
    assert mock_cp.call_count == 3
    mock_cp.assert_any_call(worktree, "aaa111")
    mock_cp.assert_any_call(worktree, "bbb222")
    mock_cp.assert_any_call(worktree, "ccc333")


def test_recover_with_cherry_pick_conflict_on_second_commit_returns_hash(tmp_path):
    """Conflict on 2nd cherry-pick -> returns the failing commit hash."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    commits = ["aaa111", "bbb222", "ccc333"]

    call_count = [0]

    def mock_cp_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            return MagicMock(returncode=1)
        return MagicMock(returncode=0)

    mock_reset = MagicMock()
    mock_cp = MagicMock(side_effect=mock_cp_side_effect)
    mock_log = MagicMock(return_value="hash some message")

    with patch("ow.commands.rebase.git_reset_hard", mock_reset), \
         patch("ow.commands.rebase.git_cherry_pick", mock_cp), \
         patch("ow.commands.rebase.git_log_oneline", mock_log):
        result = _recover_with_cherry_pick(worktree, "origin/master", commits)

    assert result == "bbb222"
    assert mock_cp.call_count == 2  # stops after the conflict


# ---------------------------------------------------------------------------
# _analyze_repo_for_rebase
# ---------------------------------------------------------------------------

def test_analyze_repo_normal_rebase_no_rewrite(tmp_path):
    """Normal rebase: no upstream rewrite, no conflicts."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    with patch("ow.commands.rebase.get_rev_list_count") as mock_rev_count, \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None), \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        mock_rev_count.side_effect = [(3, True), (0, True)]  # local=3, unpushed=0

        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.alias == "community"
    assert plan.track_ref == "origin/master"
    assert plan.upstream == "origin/master"
    assert plan.is_detached is False
    assert plan.local_commits == 3
    assert plan.unpushed_commits == 0
    assert plan.fork_point is None
    assert plan.commits_to_reapply == []
    assert plan.upstream_rewritten is False
    assert plan.has_conflicts is False


def test_analyze_repo_upstream_rewritten_with_fork_point(tmp_path):
    """Upstream rewritten but fork-point exists -> recovery possible."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    fork = "abc123"
    commits_list = ["def456", "ghi789"]

    with patch("ow.commands.rebase.get_rev_list_count") as mock_rev_count, \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=fork), \
         patch("ow.commands.rebase.git_rev_list", return_value=commits_list), \
         patch("ow.commands.rebase.get_worktree_branch", return_value="my-feature"):
        mock_rev_count.side_effect = [(2, True), (2, True)]  # local=2, unpushed=2

        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.fork_point == fork
    assert plan.commits_to_reapply == commits_list
    assert plan.upstream_rewritten is False  # fork_point found, so not "rewritten"
    assert plan.unpushed_commits == 2


def test_analyze_repo_upstream_rewritten_without_fork_point(tmp_path):
    """Upstream rewritten and no fork-point -> no recovery."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    with patch("ow.commands.rebase.get_rev_list_count") as mock_rev_count, \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None), \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.commands.rebase.get_worktree_branch", return_value="my-feature"):
        mock_rev_count.side_effect = [(2, True), (2, True)]  # local=2, unpushed=2

        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.fork_point is None
    assert plan.commits_to_reapply == []
    assert plan.upstream_rewritten is True  # no fork_point AND unpushed > 0
    assert plan.unpushed_commits == 2


def test_analyze_repo_rebase_in_progress(tmp_path):
    """rebase-merge directory exists -> has_conflicts."""
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)
    (worktree / ".git" / "rebase-merge").mkdir()

    with patch("ow.commands.rebase.get_rev_list_count", return_value=(1, True)), \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None), \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value="my-feature"):
        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", False)

    assert plan.has_conflicts is True


def test_analyze_repo_detached_worktree(tmp_path):
    """Detached worktree -> is_detached True, no fork-point lookup."""
    worktree = tmp_path / "repo"
    worktree.mkdir()

    with patch("ow.commands.rebase.get_rev_list_count", return_value=(0, True)), \
         patch("ow.commands.rebase.git_merge_base_fork_point", return_value=None) as mock_fork, \
         patch("ow.commands.rebase.git_rev_list", return_value=[]), \
         patch("ow.utils.drift.get_worktree_branch", return_value=None):
        plan = _analyze_repo_for_rebase(worktree, "origin/master", "origin/master", "community", True)

    assert plan.is_detached is True
    assert mock_fork.call_count == 0
