"""`ow pull` on real git repositories.

What pull promises is that it can never lose a commit: it either
fast-forwards or refuses. Only a real repository can show that, so the git
side is never mocked here — the fetch is, since there is no remote to talk
to.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ow.commands.pull import cmd_pull
from ow.utils import paths
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    parse_branch_spec,
    write_workspace_config,
)
from ow.utils.refs import FetchOutcome


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _bare_repo(tmp_path: Path, alias: str = "community") -> Path:
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "A")

    repos = paths.repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{alias}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        capture_output=True, text=True, check=True,
    )
    _git(bare, "config", "user.email", "t@t")
    _git(bare, "config", "user.name", "T")
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare


def _commit(worktree: Path, name: str) -> str:
    (worktree / f"{name}.txt").write_text(name)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", name)
    return _git(worktree, "rev-parse", "HEAD")


def _workspace(tmp_path: Path, spec: str = "master..featA") -> tuple[Config, Path, Path]:
    """A workspace whose `community` worktree is one commit behind origin/featA."""
    bare = _bare_repo(tmp_path)
    ws_dir = tmp_path / "workspaces" / "test"
    worktree = ws_dir / "community"
    ws_dir.mkdir(parents=True)
    _git(bare, "worktree", "add", "-q", str(worktree), "-b", "featA", "master")

    ahead = _commit(worktree, "B")
    _git(worktree, "update-ref", "refs/remotes/origin/featA", ahead)
    _git(worktree, "reset", "-q", "--hard", "HEAD~1")

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec(spec)}),
    )
    return Config(remotes={}), ws_dir, worktree


def _fetched(tracks: dict[str, str], upstreams: dict[str, str] | None = None, failed=frozenset()):
    upstreams = upstreams or {}
    return FetchOutcome(
        tracks=tracks,
        upstreams=upstreams,
        specs={a: BranchSpec(t) for a, t in tracks.items()},
        upstream_before={},
        failed=failed,
    )


def _run_pull(config, ws_dir, outcome, **kwargs):
    from ow.utils.config import load_workspace_config

    ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    with (
        patch("ow.commands.pull.resolve_workspace", return_value=(ws_dir, ws)),
        patch("ow.commands.pull.warn_if_drifted"),
        patch("ow.commands.pull.fetch_workspace_refs", return_value=outcome),
    ):
        cmd_pull(config, workspace=None, **kwargs)


def test_a_branch_behind_its_upstream_is_fast_forwarded(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    upstream = _git(worktree, "rev-parse", "refs/remotes/origin/featA")

    _run_pull(config, ws_dir, _fetched({"community": "origin/master"}, {"community": "origin/featA"}))

    assert _git(worktree, "rev-parse", "HEAD") == upstream
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert "fast-forward" in capsys.readouterr().out


def test_diverging_from_its_own_upstream_replays_the_local_work_on_top(tmp_path, capsys, xdg):
    """The reported case: a branch 23 ahead and 14 behind its own remote copy.
    `git pull --rebase` handles it; refusing it made ow pull useless there."""
    config, ws_dir, worktree = _workspace(tmp_path)
    outcome = _fetched({"community": "origin/master"}, {"community": "origin/featA"})
    _run_pull(config, ws_dir, outcome)

    _commit(worktree, "C")
    # A commit on the upstream that is not on our branch.
    _git(worktree, "checkout", "-q", "--detach", "HEAD~1")
    theirs = _commit(worktree, "D")
    _git(worktree, "update-ref", "refs/remotes/origin/featA", theirs)
    _git(worktree, "switch", "-q", "featA")

    _run_pull(config, ws_dir, outcome)

    subjects = _git(worktree, "log", "--format=%s", "-3").splitlines()
    assert subjects[:2] == ["C", "D"]
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert _git(worktree, "merge-base", "--is-ancestor", theirs, "HEAD") == ""


def test_diverging_from_the_base_ref_is_refused_and_nothing_moves(tmp_path, capsys, xdg):
    """No upstream: carrying local work over to a moved base is the big move,
    with force-push detection and a replay floor. That is ow rebase's."""
    config, ws_dir, worktree = _workspace(tmp_path)
    mine = _commit(worktree, "C")
    # Move the base to a commit our branch does not contain.
    _git(worktree, "checkout", "-q", "--detach", "HEAD~1")
    theirs = _commit(worktree, "D")
    _git(worktree, "update-ref", "refs/remotes/origin/master", theirs)
    _git(worktree, "switch", "-q", "featA")

    with pytest.raises(SystemExit) as exit_info:
        _run_pull(config, ws_dir, _fetched({"community": "origin/master"}))

    assert exit_info.value.code == 1
    assert _git(worktree, "rev-parse", "HEAD") == mine
    assert "ow rebase" in capsys.readouterr().err


def test_a_branch_merely_ahead_of_its_base_is_up_to_date(tmp_path, capsys, xdg):
    """Unpushed work on an unmoved base is the normal state of a feature
    branch, and pull used to call every one of them diverged."""
    config, ws_dir, worktree = _workspace(tmp_path)
    mine = _commit(worktree, "C")

    _run_pull(config, ws_dir, _fetched({"community": "origin/master"}))

    assert _git(worktree, "rev-parse", "HEAD") == mine
    assert "up to date" in capsys.readouterr().out


def test_a_dirty_worktree_stops_a_replay_before_git_refuses_it(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    outcome = _fetched({"community": "origin/master"}, {"community": "origin/featA"})
    _run_pull(config, ws_dir, outcome)

    mine = _commit(worktree, "C")
    _git(worktree, "checkout", "-q", "--detach", "HEAD~1")
    theirs = _commit(worktree, "D")
    _git(worktree, "update-ref", "refs/remotes/origin/featA", theirs)
    _git(worktree, "switch", "-q", "featA")
    (worktree / "C.txt").write_text("edited, not committed")

    with pytest.raises(SystemExit):
        _run_pull(config, ws_dir, outcome)

    assert _git(worktree, "rev-parse", "HEAD") == mine
    assert "C.txt" in capsys.readouterr().err


def test_an_already_current_repo_is_left_alone(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    outcome = _fetched({"community": "origin/master"}, {"community": "origin/featA"})
    _run_pull(config, ws_dir, outcome)
    settled = _git(worktree, "rev-parse", "HEAD")

    _run_pull(config, ws_dir, outcome)

    assert _git(worktree, "rev-parse", "HEAD") == settled
    assert "up to date" in capsys.readouterr().out


def test_a_detached_repo_follows_its_base_ref(tmp_path, capsys, xdg):
    """A detached spec has no branch to fast-forward, so it re-detaches."""
    bare = _bare_repo(tmp_path)
    ws_dir = tmp_path / "workspaces" / "test"
    worktree = ws_dir / "community"
    ws_dir.mkdir(parents=True)
    _git(bare, "worktree", "add", "-q", "--detach", str(worktree), "master")

    _git(worktree, "switch", "-q", "-c", "scratch")
    ahead = _commit(worktree, "B")
    _git(worktree, "update-ref", "refs/remotes/origin/master", ahead)
    _git(worktree, "checkout", "-q", "--detach", "HEAD~1")
    _git(worktree, "branch", "-q", "-D", "scratch")

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec("master")}),
    )

    _run_pull(Config(remotes={}), ws_dir, _fetched({"community": "origin/master"}))

    assert _git(worktree, "rev-parse", "HEAD") == ahead
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_a_stale_ref_is_never_pulled_onto(tmp_path, capsys, xdg):
    """Fast-forwarding to the cached ref after a failed fetch would report
    success for work the remote never handed over."""
    config, ws_dir, worktree = _workspace(tmp_path)
    before = _git(worktree, "rev-parse", "HEAD")

    with pytest.raises(SystemExit) as exit_info:
        _run_pull(
            config, ws_dir,
            _fetched({"community": "origin/master"}, {"community": "origin/featA"},
                     failed=frozenset({"community"})),
        )

    assert exit_info.value.code == 1
    assert _git(worktree, "rev-parse", "HEAD") == before
    assert "refs are stale" in capsys.readouterr().err


def test_dry_run_shows_the_command_and_runs_nothing(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    before = _git(worktree, "rev-parse", "HEAD")

    _run_pull(
        config, ws_dir,
        _fetched({"community": "origin/master"}, {"community": "origin/featA"}),
        dry_run=True,
    )

    assert _git(worktree, "rev-parse", "HEAD") == before
    assert "git merge --ff-only origin/featA" in capsys.readouterr().out


def test_without_an_upstream_the_base_ref_is_what_gets_followed(tmp_path, capsys, xdg):
    """A branch nobody has pushed still wants the base it was cut from."""
    config, ws_dir, worktree = _workspace(tmp_path)
    ahead = _git(worktree, "rev-parse", "refs/remotes/origin/featA")
    _git(worktree, "update-ref", "refs/remotes/origin/master", ahead)

    _run_pull(config, ws_dir, _fetched({"community": "origin/master"}))

    assert _git(worktree, "rev-parse", "HEAD") == ahead


def test_only_narrows_the_work_to_one_repo(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    other = _bare_repo(tmp_path, "enterprise")
    other_worktree = ws_dir / "enterprise"
    _git(other, "worktree", "add", "-q", str(other_worktree), "-b", "featA", "master")

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(
            repos={
                "community": parse_branch_spec("master..featA"),
                "enterprise": parse_branch_spec("master..featA"),
            },
        ),
    )

    _run_pull(
        config, ws_dir,
        _fetched({"community": "origin/master"}, {"community": "origin/featA"}),
        only="community",
    )

    out = capsys.readouterr().out
    assert "community" in out
    assert "enterprise" not in out


def test_a_repo_mid_merge_outside_the_narrowing_blocks_the_refresh(tmp_path, capsys, xdg):
    """`--only` narrows the Git half, never the inspection: a repo the run
    did not touch still gets inspected before anything is written, so a
    workspace with an operation in progress is never rendered on top of."""
    config, ws_dir, worktree = _workspace(tmp_path)
    upstream = _git(worktree, "rev-parse", "refs/remotes/origin/featA")

    other = _bare_repo(tmp_path, "enterprise")
    other_worktree = ws_dir / "enterprise"
    _git(other, "worktree", "add", "-q", str(other_worktree), "-b", "featA", "master")
    _git(other_worktree, "switch", "-q", "-c", "conflict", "master")
    (other_worktree / "a.txt").write_text("theirs")
    _git(other_worktree, "commit", "-qam", "theirs")
    _git(other_worktree, "switch", "-q", "featA")
    (other_worktree / "a.txt").write_text("mine")
    _git(other_worktree, "commit", "-qam", "mine")
    # A real, conflicted merge: MERGE_HEAD is on disk and the worktree is
    # exactly what in_progress_operation exists to notice.
    subprocess.run(
        ["git", "-C", str(other_worktree), "merge", "conflict"],
        capture_output=True, text=True,
    )

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(
            repos={
                "community": parse_branch_spec("master..featA"),
                "enterprise": parse_branch_spec("master..featA"),
            },
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        _run_pull(
            config, ws_dir,
            _fetched({"community": "origin/master"}, {"community": "origin/featA"}),
            only="community",
        )

    assert exit_info.value.code == 1
    # The Git half really succeeded: community was fast-forwarded.
    assert _git(worktree, "rev-parse", "HEAD") == upstream
    # And nothing was generated on top of a workspace that is not ready.
    assert not (ws_dir / ".ow" / "rendered.lock.toml").exists()
    assert not (ws_dir / "odoorc").exists()
    err = capsys.readouterr().err
    assert "files were not refreshed" in err
    assert "merge in progress" in err
