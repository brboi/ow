"""`ow switch` on real git repositories.

The pre-flight is all-or-nothing (a bad repo must abort the whole run
before any repo is touched) and the config write-back is from observed
git state, not from the flags the command was given — both are only
provable against real worktrees.
"""

import subprocess
from pathlib import Path

import pytest

from ow.commands.switch import cmd_switch
from ow.utils import paths
from ow.utils.config import (
    Config,
    RemoteConfig,
    WorkspaceConfig,
    load_workspace_config,
    parse_branch_spec,
    write_workspace_config,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path, alias: str) -> tuple[Path, Path]:
    """A bare repo plus its 'origin' source, wired the way `ow apply` leaves
    one: a master branch, and a fetch refspec so refs/remotes/origin/*
    means something."""
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
    # `clone --bare` leaves no fetch refspec, so git does not recognise
    # refs/remotes/* as remote branches. ow sets one; tests need it too.
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare, src


def _add_worktree(bare: Path, ws_dir: Path, alias: str, *, branch: str = "featA", start: str = "master") -> Path:
    worktree = ws_dir / alias
    _git(bare, "worktree", "add", "-q", str(worktree), "-b", branch, start)
    return worktree


def _branch_only_on_source(src: Path, name: str, start: str = "master") -> None:
    """A branch the origin repo has but the bare clone's refs/remotes does
    not know about yet — exactly what a real push looks like before ow
    fetches it."""
    _git(src, "branch", name, start)


def _workspace_config(ws_dir: Path, repos: dict[str, str]) -> None:
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={a: parse_branch_spec(s) for a, s in repos.items()}, templates=[]),
    )


def test_a_plain_switch_moves_every_repo_and_rewrites_the_specs(tmp_path, capsys, xdg):
    bare_c, src_c = _make_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src_c, "feature-x")
    _branch_only_on_source(src_e, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = _add_worktree(bare_e, ws_dir, "enterprise")
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master..featA"})

    config = Config(vars={}, remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"

    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["community"] == parse_branch_spec("feature-x..feature-x")
    assert new_ws.repos["enterprise"] == parse_branch_spec("feature-x..feature-x")


def test_a_repo_with_no_configured_remote_still_fetches_its_own(tmp_path, capsys, xdg):
    """The global `[remotes.<alias>]` table describes what new workspaces
    get, not what a repo has: an alias missing from it must still reach the
    remotes its bare repo carries, or a branch that plainly exists is
    reported as nowhere to be found without a single fetch attempted."""
    bare, src = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "enterprise")
    _workspace_config(ws_dir, {"enterprise": "master..featA"})

    cmd_switch(Config(vars={}, remotes={}), "feature-x", workspace=str(ws_dir))

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["enterprise"] == parse_branch_spec("feature-x..feature-x")


def test_a_target_missing_in_one_repo_aborts_the_whole_switch(tmp_path, capsys, xdg):
    bare_c, src_c = _make_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src_c, "feature-x")
    # enterprise never gets feature-x anywhere.

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = _add_worktree(bare_e, ws_dir, "enterprise")
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master..featA"})

    config = Config(vars={}, remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })

    with pytest.raises(SystemExit) as exit_info:
        cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert exit_info.value.code == 2
    # Neither repo moved...
    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    # ...and the config was never touched.
    unchanged = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert unchanged.repos["community"] == parse_branch_spec("master..featA")
    assert unchanged.repos["enterprise"] == parse_branch_spec("master..featA")
    captured = capsys.readouterr()
    # The table names the repo that blocked the run; stderr says the run did
    # nothing at all, which no per-repo line can say.
    assert "enterprise" in captured.out
    assert "no branch named 'feature-x'" in captured.out
    assert "Nothing was switched" in captured.err


def test_create_makes_the_branch_from_the_given_start_point(tmp_path, capsys, xdg):
    bare, src = _make_repo(tmp_path, "community")
    _git(src, "branch", "release", "master")
    # Move release ahead of master so we can tell which one it branched from.
    (src / "r.txt").write_text("r")
    _git(src, "checkout", "-q", "release")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "R")
    _git(bare, "fetch", "-q", "origin", "release")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})

    config = Config(vars={}, remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "origin/release", workspace=str(ws_dir), create="new-feature")

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "new-feature"
    assert _git(wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "origin/release")

    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    spec = new_ws.repos["community"]
    assert spec.local_branch == "new-feature"
    assert spec.base_ref == "origin/release"


def test_detach_writes_a_bare_spec_at_the_ref_the_user_asked_for(tmp_path, capsys, xdg):
    bare, src = _make_repo(tmp_path, "community")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})

    config = Config(vars={}, remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "origin/master", workspace=str(ws_dir), detach=True)

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    spec = new_ws.repos["community"]
    assert spec.is_detached
    assert spec.base_ref == "origin/master"


def test_only_narrows_which_repos_are_touched(tmp_path, capsys, xdg):
    bare_c, src_c = _make_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src_c, "feature-x")
    _branch_only_on_source(src_e, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = _add_worktree(bare_e, ws_dir, "enterprise")
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master..featA"})

    config = Config(vars={}, remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })

    cmd_switch(config, "feature-x", workspace=str(ws_dir), only="community")

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["enterprise"] == parse_branch_spec("master..featA")


def test_dry_run_prints_the_command_and_writes_nothing(tmp_path, capsys, xdg):
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})

    config = Config(vars={}, remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir), dry_run=True)

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    unchanged = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert unchanged.repos["community"] == parse_branch_spec("master..featA")
    out = capsys.readouterr().out
    assert "git switch -c feature-x origin/feature-x" in out


def test_a_dirty_worktree_still_switches_when_git_allows_it(tmp_path, capsys, xdg):
    """`git switch` carries uncommitted changes across when it can; ow must
    not refuse on its own account."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")  # branched from master; a.txt untouched there

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")  # featA, also branched from master
    _workspace_config(ws_dir, {"community": "master..featA"})
    (wt / "a.txt").write_text("edited, uncommitted")

    config = Config(vars={}, remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert (wt / "a.txt").read_text() == "edited, uncommitted"


def test_a_repo_already_on_the_target_is_left_alone(tmp_path, capsys, xdg):
    """Switching a workspace to the branch part of it is already on must not
    re-derive the spec of the repos that never moved: an upstream ow wrote
    once would be rewritten from whatever git reports today."""
    bare, src = _make_repo(tmp_path, "community")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "some-base..featA"})
    before = (ws_dir / ".ow" / "config.toml").read_text()
    head_before = _git(wt, "rev-parse", "HEAD")

    config = Config(vars={}, remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "featA", workspace=str(ws_dir))

    assert (ws_dir / ".ow" / "config.toml").read_text() == before
    assert _git(wt, "rev-parse", "HEAD") == head_before
    out = capsys.readouterr().out
    assert "already there" in out
    assert "git switch" not in out


def test_creating_a_branch_one_repo_already_has_moves_nothing(tmp_path, capsys, xdg):
    """git refuses `switch -c` on an existing branch. Caught during execution
    that would leave the workspace half-switched, so it is caught before."""
    bare_c, src_c = _make_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = _add_worktree(bare_e, ws_dir, "enterprise")
    _git(bare_e, "branch", "feature-x", "master")  # enterprise already has it
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master..featA"})

    config = Config(vars={}, remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })

    with pytest.raises(SystemExit) as exit_info:
        cmd_switch(config, workspace=str(ws_dir), create="feature-x")

    assert exit_info.value.code == 2
    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    captured = capsys.readouterr()
    assert "already exists" in captured.out
    assert "ow switch feature-x" in captured.err


def _pin_workspace(tmp_path, capsys, xdg):  # noqa: ANN001
    """A workspace of one attached repo and one pinned (detached-spec) repo."""
    bare_c, src_c = _make_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src_c, "feature-x")
    _branch_only_on_source(src_e, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = ws_dir / "enterprise"
    _git(bare_e, "worktree", "add", "-q", "--detach", str(wt_e), "master")
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master"})

    config = Config(vars={}, remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })
    return config, ws_dir, wt_c, wt_e


def test_a_detached_spec_is_left_alone_by_default(tmp_path, capsys, xdg):
    """A bare ref in the config is a pin — the config names the exact ref that
    repo should sit on, and a run that moves the workspace's branches has no
    business rewriting it."""
    config, ws_dir, wt_c, wt_e = _pin_workspace(tmp_path, capsys, xdg)
    head_e = _git(wt_e, "rev-parse", "HEAD")

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "HEAD") == head_e
    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["enterprise"] == parse_branch_spec("master")
    out = capsys.readouterr().out
    assert "left alone" in out
    assert "--include-detached-specs" in out


def test_include_detached_specs_switches_the_pins_too(tmp_path, capsys, xdg):
    """With the flag, a pinned repo gets git's semantics like any other:
    a branch target that exists only remotely attaches it as a tracking
    branch, and the config records exactly that."""
    config, ws_dir, wt_c, wt_e = _pin_workspace(tmp_path, capsys, xdg)

    cmd_switch(config, "feature-x", workspace=str(ws_dir), include_detached=True)

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["enterprise"] == parse_branch_spec("feature-x..feature-x")
    assert "left alone" not in capsys.readouterr().out


def test_only_naming_a_pinned_repo_includes_it(tmp_path, capsys, xdg):
    """`--only` naming a repo is insisting on it: the policy must not
    silently drop the one repo the user asked for by name."""
    config, ws_dir, wt_c, wt_e = _pin_workspace(tmp_path, capsys, xdg)

    cmd_switch(config, "feature-x", workspace=str(ws_dir), only="enterprise")

    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "featA"


def test_a_workspace_of_only_pins_switches_nothing(tmp_path, capsys, xdg):
    """Every repo excluded is a run with nothing to run: it must report the
    pins and exit cleanly, not crash on an empty pre-flight."""
    bare, src = _make_repo(tmp_path, "enterprise")
    ws_dir = tmp_path / "workspaces" / "test"
    wt = ws_dir / "enterprise"
    _git(bare, "worktree", "add", "-q", "--detach", str(wt), "master")
    _workspace_config(ws_dir, {"enterprise": "master"})
    head = _git(wt, "rev-parse", "HEAD")
    before = (ws_dir / ".ow" / "config.toml").read_text()

    cmd_switch(Config(vars={}, remotes={}), "feature-x", workspace=str(ws_dir))

    assert (ws_dir / ".ow" / "config.toml").read_text() == before
    assert "left alone" in capsys.readouterr().out
