import shutil
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from ow.commands.rebase import _summary_line, cmd_rebase
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config
from ow.utils.rebase_plan import GitStep, RebasePlan, RepoFacts
from ow.utils.refs import FetchOutcome


def make_workspace(tmp_path: Path, repos: dict[str, str]) -> tuple[Config, Path]:
    ws_dir = tmp_path / "workspaces" / "test"
    for alias in repos:
        (ws_dir / alias).mkdir(parents=True)
    from ow.utils.config import parse_branch_spec
    ws = WorkspaceConfig(
        repos={a: parse_branch_spec(s) for a, s in repos.items()},
        templates=["common"],
    )
    write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
    config = Config(vars={}, remotes={})
    return config, ws_dir


def _ws(ws_dir: Path) -> WorkspaceConfig:
    from ow.utils.config import load_workspace_config
    return load_workspace_config(ws_dir / ".ow" / "config.toml")


def fetch_returning(
    tracks: dict[str, str], failed: frozenset[str] = frozenset()
) -> FetchOutcome:
    return FetchOutcome(
        tracks=tracks,
        upstreams={},
        specs={a: BranchSpec(t) for a, t in tracks.items()},
        upstream_before={},
        failed=failed,
    )


def _ok() -> CompletedProcess:
    return CompletedProcess([], 0)


def _fail() -> CompletedProcess:
    return CompletedProcess([], 1)


def _facts_with_work(worktree, alias, base, up, up_before, is_detached):
    return RepoFacts(alias=alias, base=base, bound="BOUND", base_merged=False, replay_count=2)


def _facts_two_step(worktree, alias, base, up, up_before, is_detached):
    return RepoFacts(
        alias=alias, base=base, up="dev/work", bound="BOUND",
        base_merged=True, new_patches=1, replay_count=3,
    )


def _facts_busy(worktree, alias, base, up, up_before, is_detached):
    return RepoFacts(
        alias=alias, base=base,
        busy=("rebase", "git rebase --continue", "git rebase --abort"),
    )


class TestConfirmation:
    def test_eof_aborts_and_runs_no_git(self, tmp_path, capsys):
        """A destructive command must not default to yes with no one to ask."""
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.git") as mock_git,
            patch("builtins.input", side_effect=EOFError),
        ):
            cmd_rebase(config, workspace=None)
        assert mock_git.call_count == 0
        assert "Aborted" in capsys.readouterr().out

    def test_plain_enter_aborts(self, tmp_path, capsys):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.git") as mock_git,
            patch("builtins.input", return_value=""),
        ):
            cmd_rebase(config, workspace=None)
        assert mock_git.call_count == 0

    def test_yes_flag_skips_the_prompt(self, tmp_path):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.git", return_value=_ok()) as mock_git,
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert mock_git.call_count == 1


class TestDryRun:
    def test_prints_the_commands_and_runs_nothing(self, tmp_path, capsys):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.git") as mock_git,
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
        ):
            cmd_rebase(config, workspace=None, dry_run=True)
        out = capsys.readouterr().out
        assert "git rebase origin/master" in out
        assert "[community]" in out
        assert mock_git.call_count == 0


class TestConflictReporting:
    def test_names_the_ref_the_failing_step_landed_on(self, tmp_path, capsys):
        """Defect 1.2: the message used to always name the upstream."""
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_two_step),
            patch("ow.commands.rebase.in_progress_operation",
                  return_value=("rebase", "git rebase --continue", "git rebase --abort")),
            patch("ow.commands.rebase.git", side_effect=[_ok(), _fail()]),
            patch("builtins.input", return_value="y"),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "origin/master" in err
        assert "dev/work" not in err.split("CONFLICT")[1].split("\n")[0]
        assert "ow rebase --only community" in err


class TestSkips:
    def test_a_busy_repo_is_skipped_and_counts_as_a_failure(self, tmp_path, capsys):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_busy),
            patch("ow.commands.rebase.git") as mock_git,
            patch("builtins.input", return_value="y"),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None)
        assert exc.value.code == 1
        assert mock_git.call_count == 0
        assert "git rebase --continue" in capsys.readouterr().err


class TestAnalysisFailure:
    """Finding 1 of fix round 1: a repo whose analysis breaks must be
    reported and must fail the run, not vanish with a 0 exit code."""

    def test_a_failed_analysis_is_reported_and_fails_the_run(self, tmp_path, capsys):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=RuntimeError("boom")),
            patch("ow.commands.rebase.git") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert exc.value.code == 1
        assert mock_git.call_count == 0
        err = capsys.readouterr().err
        assert "community" in err and "could not analyse" in err

    def test_a_missing_worktree_is_reported_and_fails_the_run(self, tmp_path, capsys):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        shutil.rmtree(ws_dir / "community")
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts") as mock_gather,
            patch("ow.commands.rebase.git") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert exc.value.code == 1
        mock_gather.assert_not_called()
        assert mock_git.call_count == 0
        err = capsys.readouterr().err
        assert "community" in err
        assert "worktree not found" in err


class TestMultiRepo:
    def test_a_failure_in_one_repo_does_not_stop_the_others(self, tmp_path, capsys):
        """Failure isolation, and — since each repo must have received its
        own facts to reach this point — the per-repo lambda binding."""
        config, ws_dir = make_workspace(tmp_path, {
            "community": "master..work",
            "enterprise": "master..work",
        })
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({
                      "community": "origin/master", "enterprise": "origin/master",
                  })),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.in_progress_operation",
                  return_value=("rebase", "git rebase --continue", "git rebase --abort")),
            patch("ow.commands.rebase.git", side_effect=[_fail(), _ok()]) as mock_git,
            patch("builtins.input", return_value="y"),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None)
        assert exc.value.code == 1
        assert mock_git.call_count == 2  # enterprise still ran after community's conflict
        err = capsys.readouterr().err
        conflict_line = err.split("CONFLICT")[1].split("\n")[0]
        assert "community" in conflict_line
        assert "enterprise" not in conflict_line

    def test_only_touches_the_selected_repo(self, tmp_path):
        """--only must narrow drift-checking and fetching too, not just execution."""
        config, ws_dir = make_workspace(tmp_path, {
            "community": "master..work",
            "enterprise": "master..work",
        })
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted") as mock_drift,
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({
                      "community": "origin/master", "enterprise": "origin/master",
                  })) as mock_fetch,
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.git", return_value=_ok()) as mock_git,
            patch("builtins.input", return_value="y"),
        ):
            cmd_rebase(config, workspace=None, only="community")

        touched = {call.args[0] for call in mock_git.call_args_list}
        assert touched == {ws_dir / "community"}

        drifted_ws = mock_drift.call_args.args[0]
        fetched_ws = mock_fetch.call_args.args[0]
        assert set(drifted_ws.repos) == {"community"}
        assert set(fetched_ws.repos) == {"community"}


class TestObservedShape:
    """D1 — the worktree's real shape, not the config's intent.

    gather_facts is deliberately *not* patched here: the defect was that it
    trusted the config, planned a two-step rebase against the wrong shape,
    ran it on a detached HEAD and reported success with exit 0.
    """

    def _detached_worktree(self, tmp_path: Path) -> tuple[Config, Path]:
        import subprocess
        config, ws_dir = make_workspace(tmp_path, {"community": "master..featA"})
        repo = ws_dir / "community"
        for args in (
            ["init", "-q", "-b", "master"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
        (repo / "a.txt").write_text("a")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "A"], check=True)
        subprocess.run(["git", "-C", str(repo), "branch", "featA"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach", "HEAD"], check=True)
        return config, ws_dir

    def test_a_drifted_worktree_is_skipped_and_fails_the_run(self, tmp_path, capsys):
        config, ws_dir = self._detached_worktree(tmp_path)
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.git") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert exc.value.code == 1
        assert mock_git.call_count == 0
        err = capsys.readouterr().err
        assert "Skipping community" in err
        assert "ow apply" in err


class TestFailedFetch:
    """D2 — a failed fetch left ow rebasing onto the stale cached ref.

    It exited 0, and because the upstream ref had not moved, force_pushed
    stayed False and the `rewritten` marker was suppressed too: less warning
    than a normal run, not more.
    """

    def test_an_alias_whose_fetch_failed_is_not_rebased(self, tmp_path, capsys):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning(
                      {"community": "origin/master"}, frozenset({"community"}),
                  )),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work) as mock_gather,
            patch("ow.commands.rebase.git") as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert exc.value.code == 1
        mock_gather.assert_not_called()
        assert mock_git.call_count == 0
        err = capsys.readouterr().err
        assert "community" in err and "fetch failed" in err

    def test_the_other_repos_are_still_rebased(self, tmp_path):
        config, ws_dir = make_workspace(tmp_path, {
            "community": "master..work",
            "enterprise": "master..work",
        })
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning(
                      {"community": "origin/master", "enterprise": "origin/master"},
                      frozenset({"community"}),
                  )),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.git", return_value=_ok()) as mock_git,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert exc.value.code == 1
        touched = {call.args[0] for call in mock_git.call_args_list}
        assert touched == {ws_dir / "enterprise"}


_REBASE_IN_PROGRESS = ("rebase", "git rebase --continue", "git rebase --abort")


class TestNonConflictFailures:
    """D3 — `_execute` called every non-zero exit a CONFLICT.

    A rebase that never started — no git identity, a rejecting hook, a full
    disk — got `git rebase --continue` / `--abort` prescribed at it, and both
    of those just error out when no rebase is in progress.
    """

    def _run_failing_step(self, tmp_path, in_progress):
        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_facts_with_work),
            patch("ow.commands.rebase.in_progress_operation", return_value=in_progress),
            patch("ow.commands.rebase.git", return_value=_fail()),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_rebase(config, workspace=None, yes=True)
        assert exc.value.code == 1

    def test_a_failure_with_no_rebase_in_progress_is_not_called_a_conflict(
        self, tmp_path, capsys,
    ):
        self._run_failing_step(tmp_path, None)
        err = capsys.readouterr().err
        assert "CONFLICT" not in err
        assert "rebase --continue" not in err
        assert "rebase --abort" not in err
        assert "community" in err
        assert "origin/master" in err

    def test_a_real_conflict_still_gets_the_resume_advice(self, tmp_path, capsys):
        self._run_failing_step(tmp_path, _REBASE_IN_PROGRESS)
        err = capsys.readouterr().err
        assert "CONFLICT" in err
        assert "git rebase --continue" in err
        assert "git rebase --abort" in err


class TestDryRunWithNothingToDo:
    """D5 — a bare `Would run:` header with nothing under it."""

    def test_says_nothing_to_do_when_every_repo_is_up_to_date(self, tmp_path, capsys):
        def _noop_facts(worktree, alias, base, up, up_before, is_detached):
            return RepoFacts(alias=alias, base=base, bound="BOUND", base_merged=True)

        config, ws_dir = make_workspace(tmp_path, {"community": "master..work"})
        with (
            patch("ow.commands.rebase.resolve_workspace", return_value=(ws_dir, _ws(ws_dir))),
            patch("ow.commands.rebase.warn_if_drifted"),
            patch("ow.commands.rebase.fetch_workspace_refs",
                  return_value=fetch_returning({"community": "origin/master"})),
            patch("ow.commands.rebase.gather_facts", side_effect=_noop_facts),
            patch("ow.commands.rebase.git") as mock_git,
        ):
            cmd_rebase(config, workspace=None, dry_run=True)
        out = capsys.readouterr().out
        assert "nothing to do" in out
        assert mock_git.call_count == 0


class TestSummaryLine:
    """The screen the user reads before typing `y` on a destructive command.

    Nothing asserted a single word of it: `commit(s) to replay`, the
    `rewritten` and `unpushed` markers, the step-1 target and the skip
    reason were all invisible to the suite, so the whole unpushed-count fix
    could have been reverted with the suite still green.
    """

    def test_summary_line_reports_the_work_and_the_markers(self):
        plan = RebasePlan(
            alias="community", base="origin/master",
            steps=(GitStep(("rebase", "origin/master"), "origin/master"),),
            step1_target="dev/work", replay_count=3, unpushed=2, force_pushed=True,
        )
        line = _summary_line(plan, 9)
        assert "3 commit(s) to replay" in line and "2 unpushed" in line
        assert "rewritten" in line and "dev/work" in line

    def test_summary_line_names_the_skip_reason(self):
        plan = RebasePlan(
            alias="c", base="origin/master", skip_reason="uncommitted changes: a.py",
        )
        assert "uncommitted changes: a.py" in _summary_line(plan, 1)

    def test_a_repo_with_no_steps_reads_as_up_to_date(self):
        plan = RebasePlan(alias="community", base="origin/master")
        line = _summary_line(plan, 9)
        assert "up to date" in line
        assert "detach" not in line

    def test_a_detaching_plan_reads_as_a_detach(self):
        plan = RebasePlan(
            alias="community", base="origin/master",
            steps=(GitStep(("switch", "--detach", "origin/master"), "origin/master"),),
        )
        line = _summary_line(plan, 9)
        assert "detach" in line
        assert "up to date" not in line

    def test_a_clean_plan_carries_no_markers(self):
        plan = RebasePlan(
            alias="community", base="origin/master",
            steps=(GitStep(("rebase", "origin/master"), "origin/master"),),
            replay_count=1,
        )
        line = _summary_line(plan, 9)
        assert "rewritten" not in line and "unpushed" not in line

    def test_a_single_step_plan_names_only_the_base(self):
        plan = RebasePlan(
            alias="community", base="origin/master",
            steps=(GitStep(("rebase", "origin/master"), "origin/master"),),
            replay_count=1,
        )
        assert "←" not in _summary_line(plan, 9)
