import shutil
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from ow.commands.rebase import _select_aliases, cmd_rebase
from ow.utils.config import BranchSpec, Config, WorkspaceConfig, write_workspace_config
from ow.utils.rebase_plan import RepoFacts
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


def fetch_returning(tracks: dict[str, str]) -> FetchOutcome:
    return FetchOutcome(
        tracks=tracks,
        upstreams={},
        specs={a: BranchSpec(t) for a, t in tracks.items()},
        upstream_before={},
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


class TestSelectAliases:
    def test_none_selects_everything(self):
        assert _select_aliases(["a", "b"], None) == ["a", "b"]

    def test_only_filters_and_preserves_config_order(self):
        assert _select_aliases(["a", "b", "c"], "c,a") == ["a", "c"]

    def test_only_tolerates_spaces(self):
        assert _select_aliases(["a", "b"], " a , b ") == ["a", "b"]

    def test_unknown_alias_raises_and_lists_the_valid_ones(self):
        import typer
        with pytest.raises(typer.BadParameter) as exc:
            _select_aliases(["a", "b"], "nope")
        assert "nope" in str(exc.value)
        assert "a, b" in str(exc.value)


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
