"""`ow switch` on real git repositories.

The pre-flight is all-or-nothing (a bad repo must abort the whole run
before any repo is touched) and the config write-back is from observed
git state, not from the flags the command was given — both are only
provable against real worktrees.
"""

import configparser
import subprocess
from pathlib import Path
from unittest.mock import patch

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


@pytest.fixture(autouse=True)
def _mise_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh boundary's one prerequisite check, stubbed the way every
    sibling suite stubs it: the host's mise version is not the behavior
    under test. Everything past the gate — inspection, generation, the
    write, `trust=False` — stays real."""
    monkeypatch.setattr("ow.utils.workspace.require_mise", lambda: (2026, 9, 9))


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
        WorkspaceConfig(repos={a: parse_branch_spec(s) for a, s in repos.items()}),
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

    config = Config(remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"

    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["community"] == parse_branch_spec("feature-x..feature-x")
    assert new_ws.repos["enterprise"] == parse_branch_spec("feature-x..feature-x")


def test_a_switch_on_a_schema_1_workspace_keeps_it_schema_1(tmp_path, capsys, xdg):
    """Switch rewrites the specs git was given; it does not migrate the file.

    A schema-1 manifest can hold sections ow has no model for — the bundles
    it declared, the vars it was rendered with. Converting it is `ow render`'s
    explicit decision, and dropping what it says on the way past would be
    exactly the silent data loss the migration exists to avoid."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    _add_worktree(bare, ws_dir, "community")
    marker = ws_dir / ".ow" / "config.toml"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        'templates = ["common"]\n\n[repos]\ncommunity = "master..featA"\n\n'
        '[vars]\nmystery = "kept"\n'
    )

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    content = marker.read_text()
    assert "version" not in content
    assert 'templates = ["common"]' in content
    assert 'mystery = "kept"' in content
    reloaded = load_workspace_config(marker)
    assert reloaded.version == 1
    assert reloaded.repos["community"] == parse_branch_spec("feature-x..feature-x")
    assert "Pending migration" in capsys.readouterr().err


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

    cmd_switch(Config(remotes={}), "feature-x", workspace=str(ws_dir))

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

    config = Config(remotes={
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

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

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

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

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

    config = Config(remotes={
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

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir), dry_run=True)

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    unchanged = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert unchanged.repos["community"] == parse_branch_spec("master..featA")
    out = capsys.readouterr().out
    assert "git switch -c feature-x origin/feature-x" in out
    assert "Files were not refreshed" in out
    assert "ow render" in out


def test_a_dirty_worktree_still_switches_when_git_allows_it(tmp_path, capsys, xdg):
    """`git switch` carries uncommitted changes across when it can; ow must
    not refuse on its own account."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")  # branched from master; a.txt untouched there

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")  # featA, also branched from master
    _workspace_config(ws_dir, {"community": "master..featA"})
    (wt / "a.txt").write_text("edited, uncommitted")

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

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

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "featA", workspace=str(ws_dir))

    assert (ws_dir / ".ow" / "config.toml").read_text() == before
    assert _git(wt, "rev-parse", "HEAD") == head_before
    out = capsys.readouterr().out
    assert "already there" in out
    assert "git switch" not in out
    # Nothing moved, so nothing was refreshed: the run says so and points
    # at the command that would, instead of leaving the user guessing.
    assert "Files were not refreshed" in out
    assert "ow render" in out
    assert "Generated files refreshed." not in out


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

    config = Config(remotes={
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

    config = Config(remotes={
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

    cmd_switch(Config(remotes={}), "feature-x", workspace=str(ws_dir))

    assert (ws_dir / ".ow" / "config.toml").read_text() == before
    assert "left alone" in capsys.readouterr().out


def test_detaching_at_a_remote_only_branch_detaches_at_the_remote_ref(tmp_path, capsys, xdg):
    """git's own guess turns `switch --detach feature-x` into a branch
    creation and then refuses it ("'--detach' cannot be used with -b"), so
    ow names the remote-tracking ref itself — and pins the repo to that ref,
    not to the short name, which would mean origin's."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})
    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir), detach=True)

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "refs/remotes/origin/feature-x")
    spec = load_workspace_config(ws_dir / ".ow" / "config.toml").repos["community"]
    assert spec.is_detached
    assert spec.base_ref == "origin/feature-x"


def test_creating_from_a_remote_only_start_point(tmp_path, capsys, xdg):
    """git guesses nothing for a start point either: `-c fix feature-x`
    fails outright unless the short name resolves locally."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})
    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir), create="fix")

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "fix"
    assert _git(wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "refs/remotes/origin/feature-x")
    spec = load_workspace_config(ws_dir / ".ow" / "config.toml").repos["community"]
    assert spec.local_branch == "fix"
    assert spec.base_ref == "origin/feature-x"


def _second_remote(tmp_path, bare: Path, src: Path, *, name: str, branch: str) -> Path:
    """A second remote carrying a branch `origin` does not have.

    Everything else in this file is single-remote, which is precisely the
    case where writing the short name instead of the resolved ref is
    invisible: `feature-x` re-reads as `origin/feature-x`. Under another
    remote it re-reads as the wrong ref entirely."""
    other = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(src), str(other)], check=True)
    _git(other, "branch", branch, "master")
    _git(bare, "remote", "add", name, str(other))
    return other


def test_detaching_through_a_non_origin_remote_pins_that_remotes_ref(tmp_path, capsys, xdg):
    """The config records where the repo actually sits. `feature-x` found on
    `upstream` must be written `upstream/feature-x`: the bare short name
    re-reads as origin's branch, and a later `ow apply` or `ow reset` would
    move the repo somewhere it has never been."""
    bare, src = _make_repo(tmp_path, "community")
    _second_remote(tmp_path, bare, src, name="upstream", branch="feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})

    cmd_switch(Config(remotes={}), "feature-x", workspace=str(ws_dir), detach=True)

    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(wt, "rev-parse", "HEAD") == _git(wt, "rev-parse", "refs/remotes/upstream/feature-x")
    spec = load_workspace_config(ws_dir / ".ow" / "config.toml").repos["community"]
    assert spec.is_detached
    assert spec.base_ref == "upstream/feature-x"


def _two_repo_workspace(tmp_path):  # noqa: ANN001
    bare_c, src_c = _make_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src_c, "feature-x")
    _branch_only_on_source(src_e, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = _add_worktree(bare_e, ws_dir, "enterprise")
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master..featA"})
    config = Config(remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })
    return config, ws_dir, wt_c, wt_e


def test_running_from_inside_a_repo_switches_only_that_repo(tmp_path, capsys, xdg, monkeypatch):
    """`cd community && ow switch feature-x` is the question git would have
    answered there; answering it for every repo moves ones nobody named."""
    config, ws_dir, wt_c, wt_e = _two_repo_workspace(tmp_path)
    (wt_c / "addons").mkdir()
    monkeypatch.chdir(wt_c / "addons")

    cmd_switch(config, "feature-x")

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert "narrowed to community" in capsys.readouterr().out


def test_running_from_the_workspace_root_switches_everything(tmp_path, capsys, xdg, monkeypatch):
    config, ws_dir, wt_c, wt_e = _two_repo_workspace(tmp_path)
    monkeypatch.chdir(ws_dir)

    cmd_switch(config, "feature-x")

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert "narrowed to" not in capsys.readouterr().out


def test_all_overrides_the_narrowing(tmp_path, capsys, xdg, monkeypatch):
    """The escape hatch: standing in a repo must not cost the whole-workspace
    switch, which is what every `ow switch` did before narrowing existed."""
    config, ws_dir, wt_c, wt_e = _two_repo_workspace(tmp_path)
    monkeypatch.chdir(wt_c)

    cmd_switch(config, "feature-x", all_repos=True)

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"


def test_all_with_only_is_refused_before_anything_is_resolved(tmp_path, capsys, xdg, monkeypatch):
    """One says every repo, the other says these repos. Guessing which the
    user meant would move repos on a coin toss, so neither runs."""
    config, ws_dir, wt_c, wt_e = _two_repo_workspace(tmp_path)
    monkeypatch.chdir(wt_c)

    with pytest.raises(SystemExit) as exc:
        cmd_switch(config, "feature-x", only="community", all_repos=True)

    assert exc.value.code == 2
    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "featA"


def test_a_named_workspace_is_never_narrowed_by_the_cwd(tmp_path, capsys, xdg, monkeypatch):
    """The TUI always passes a path, and so does `-w`: naming a workspace
    asks about all of it, whatever directory the shell happens to be in."""
    config, ws_dir, wt_c, wt_e = _two_repo_workspace(tmp_path)
    monkeypatch.chdir(wt_c)

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"


def test_standing_in_a_pinned_repo_is_not_insisting_on_it(tmp_path, capsys, xdg, monkeypatch):
    """`--only enterprise` insists; being in its directory only narrows. A
    pin moved by a cd is a pin nobody asked to move."""
    config, ws_dir, wt_c, wt_e = _pin_workspace(tmp_path, capsys, xdg)
    head_e = _git(wt_e, "rev-parse", "HEAD")
    monkeypatch.chdir(wt_e)

    cmd_switch(config, "feature-x")

    assert _git(wt_e, "rev-parse", "HEAD") == head_e
    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    out = capsys.readouterr().out
    assert "left alone" in out
    assert "--include-detached-specs" in out


def _make_core_repo(tmp_path: Path, alias: str = "community") -> tuple[Path, Path]:
    """Like `_make_repo`, but the source also carries Odoo core markers."""
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "odoo-bin").write_text("#!/usr/bin/env python3\n")
    (src / "addons").mkdir()
    (src / "addons" / ".keep").write_text("")
    (src / "odoo" / "addons").mkdir(parents=True)
    (src / "odoo" / "addons" / ".keep").write_text("")
    (src / "odoo" / "release.py").write_text(
        "version_info = (19, 0, 0, 'final', 0, '')\n"
        "MIN_PY_VERSION = (3, 10)\nMAX_PY_VERSION = (3, 14)\n"
    )
    (src / "odoo" / "tools").mkdir(parents=True)
    (src / "odoo" / "tools" / "config.py").write_text(
        "PARSER.add_argument('--with-demo', action='store_true')\n"
        "PARSER.add_argument('--without-demo', action='store_true')\n"
    )
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "core")

    repos = paths.repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{alias}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        capture_output=True, text=True, check=True,
    )
    _git(bare, "config", "user.email", "t@t")
    _git(bare, "config", "user.name", "T")
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare, src


def test_a_successful_switch_still_fails_the_run_when_files_cannot_be_refreshed(tmp_path, capsys, xdg):
    """Git and the config write both succeed; a missing mise prerequisite
    must not pretend the switch itself failed, and must not throw away
    what git and the config already did — but the run still has to fail,
    honestly, for the reason that is actually true."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt = _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    with patch(
        "ow.utils.workspace.require_mise",
        side_effect=ValueError("mise 2026.8.13 or newer is required; could not run it"),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_switch(config, "feature-x", workspace=str(ws_dir))

    assert exc.value.code == 1
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    new_ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    assert new_ws.repos["community"] == parse_branch_spec("feature-x..feature-x")

    captured = capsys.readouterr()
    assert "Done." in captured.out
    assert "files were not refreshed" in captured.err


def test_only_narrowing_the_core_still_renders_every_repos_addons(tmp_path, capsys, xdg):
    """`--only` narrows which repo's Git moves; the refresh that follows
    still inspects the whole workspace, so `enterprise`'s addon path is
    not dropped just because only `community` was switched."""
    bare_c, src_c = _make_core_repo(tmp_path, "community")
    bare_e, src_e = _make_repo(tmp_path, "enterprise")
    _branch_only_on_source(src_c, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    wt_c = _add_worktree(bare_c, ws_dir, "community")
    wt_e = _add_worktree(bare_e, ws_dir, "enterprise")
    (wt_e / "my_addon").mkdir()
    (wt_e / "my_addon" / "__manifest__.py").write_text("{}")
    _workspace_config(ws_dir, {"community": "master..featA", "enterprise": "master..featA"})

    config = Config(remotes={
        "community": {"origin": RemoteConfig(url=str(src_c))},
        "enterprise": {"origin": RemoteConfig(url=str(src_e))},
    })

    cmd_switch(config, "feature-x", workspace=str(ws_dir), only="community")

    assert _git(wt_c, "rev-parse", "--abbrev-ref", "HEAD") == "feature-x"
    assert _git(wt_e, "rev-parse", "--abbrev-ref", "HEAD") == "featA"  # untouched by --only

    odoorc = configparser.ConfigParser(interpolation=None)
    odoorc.read(ws_dir / "odoorc")
    addon_paths = odoorc["options"]["addons_path"].split(",")
    assert "enterprise" in addon_paths


def test_a_successful_switch_announces_the_refresh_it_ran(tmp_path, capsys, xdg):
    """The old note said a switch never re-renders anything; what replaced
    it has to be visible: the run that refreshed the files says so, on the
    same stdout the switch itself reported on."""
    bare, src = _make_repo(tmp_path, "community")
    _branch_only_on_source(src, "feature-x")

    ws_dir = tmp_path / "workspaces" / "test"
    _add_worktree(bare, ws_dir, "community")
    _workspace_config(ws_dir, {"community": "master..featA"})

    config = Config(remotes={"community": {"origin": RemoteConfig(url=str(src))}})

    cmd_switch(config, "feature-x", workspace=str(ws_dir))

    out = capsys.readouterr().out
    assert "Generated files refreshed." in out
    assert "Files were not refreshed" not in out
    assert (ws_dir / ".ow" / "rendered.lock.toml").exists()
