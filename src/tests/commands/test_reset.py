"""`ow reset` on real git repositories.

The command exists to destroy things, so what it destroys — and, for the
plain form, what it deliberately does not — is only provable against a
real working tree. Nothing here is mocked: with no fetch, resolution is
entirely local.
"""

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

from ow.commands.reset import cmd_reset
from ow.utils import paths
from ow.utils.config import (
    Config,
    WorkspaceConfig,
    parse_branch_spec,
    write_workspace_config,
)


@pytest.fixture(autouse=True)
def _mise_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh boundary's one prerequisite check, stubbed the way every
    sibling suite stubs it: the host's mise version is not the behavior
    under test. Everything past the gate stays real."""
    monkeypatch.setattr("ow.utils.workspace.require_mise", lambda: (2026, 9, 9))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _bare_repo(
    tmp_path: Path, alias: str = "community", *, odoo_core: bool = False, addon: str | None = None,
) -> Path:
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a")
    if odoo_core:
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
    if addon is not None:
        (src / addon).mkdir()
        (src / addon / "__manifest__.py").write_text("{}")
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
    return bare


def _commit(worktree: Path, name: str) -> str:
    (worktree / f"{name}.txt").write_text(name)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", name)
    return _git(worktree, "rev-parse", "HEAD")


def _workspace(tmp_path: Path, spec: str = "master..featA") -> tuple[Config, Path, Path]:
    """A workspace whose `community` worktree sits on featA, at origin/master."""
    bare = _bare_repo(tmp_path)
    ws_dir = tmp_path / "workspaces" / "test"
    worktree = ws_dir / "community"
    ws_dir.mkdir(parents=True)
    if spec == "master":
        _git(bare, "worktree", "add", "-q", "--detach", str(worktree), "master")
    else:
        _git(bare, "worktree", "add", "-q", str(worktree), "-b", "featA", "master")

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec(spec)}),
    )
    return Config(remotes={}), ws_dir, worktree


def _refuse_input(monkeypatch, reason: str = "reset must not prompt here"):
    def _boom(*_args, **_kwargs):
        raise AssertionError(reason)

    monkeypatch.setattr("builtins.input", _boom)


def _answer(monkeypatch, reply: str):
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: reply)


def _status(worktree: Path) -> list[str]:
    return _git(worktree, "status", "--porcelain").splitlines()


def test_the_plain_form_drops_the_commits_and_keeps_their_content_on_disk(tmp_path, capsys, xdg):
    """Its whole safety story: after a plain reset nothing is gone from the
    disk — the dropped commit's work is sitting there, unstaged."""
    config, ws_dir, worktree = _workspace(tmp_path)
    base = _git(worktree, "rev-parse", "refs/remotes/origin/master")
    _commit(worktree, "C")

    cmd_reset(config, workspace=str(ws_dir), yes=True)

    assert _git(worktree, "rev-parse", "HEAD") == base
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "featA"
    assert (worktree / "C.txt").read_text() == "C"


def test_the_plain_form_never_touches_the_working_tree(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    _commit(worktree, "C")
    (worktree / "a.txt").write_text("edited")

    cmd_reset(config, workspace=str(ws_dir), yes=True)

    assert (worktree / "a.txt").read_text() == "edited"


def test_hard_discards_the_working_tree_as_well(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    base = _git(worktree, "rev-parse", "refs/remotes/origin/master")
    _commit(worktree, "C")
    (worktree / "a.txt").write_text("edited")

    cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert _git(worktree, "rev-parse", "HEAD") == base
    assert (worktree / "a.txt").read_text() == "a"
    assert not (worktree / "C.txt").exists()


def test_hard_leaves_untracked_files_alone(tmp_path, capsys, xdg):
    """Exactly as `git reset --hard` leaves them. `git clean` is its own command."""
    config, ws_dir, worktree = _workspace(tmp_path)
    _commit(worktree, "C")
    (worktree / "scratch.txt").write_text("mine")

    cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert (worktree / "scratch.txt").read_text() == "mine"


def test_a_branch_is_reset_to_its_upstream_not_to_its_base(tmp_path, capsys, xdg):
    """The reported case: an upstream that was force-pushed. Resetting onto
    the base ref instead would throw the whole branch away and leave the
    working tree miles from HEAD."""
    config, ws_dir, worktree = _workspace(tmp_path)
    pushed = _commit(worktree, "C")
    _git(worktree, "update-ref", "refs/remotes/origin/featA", pushed)
    _git(worktree, "branch", "--set-upstream-to=origin/featA", "featA")
    _commit(worktree, "D")

    cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert _git(worktree, "rev-parse", "HEAD") == pushed
    assert "origin/featA" in capsys.readouterr().out


def test_without_an_upstream_the_base_ref_is_what_it_falls_back_to(tmp_path, capsys, xdg):
    """A branch nobody has pushed has no remote copy to go back to."""
    config, ws_dir, worktree = _workspace(tmp_path)
    base = _git(worktree, "rev-parse", "refs/remotes/origin/master")
    _commit(worktree, "C")

    cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert _git(worktree, "rev-parse", "HEAD") == base


def test_fetch_refreshes_the_ref_before_resetting(tmp_path, capsys, xdg):
    """Without it, reset lands on whatever the last fetch cached — which is
    not what a force-pushed upstream needs."""
    config, ws_dir, worktree = _workspace(tmp_path)
    src = tmp_path / "origin" / "community"
    (src / "b.txt").write_text("b")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "B")
    moved = _git(src, "rev-parse", "HEAD")
    stale = _git(worktree, "rev-parse", "HEAD")

    cmd_reset(config, workspace=str(ws_dir), yes=True)
    assert _git(worktree, "rev-parse", "HEAD") == stale

    cmd_reset(config, workspace=str(ws_dir), fetch=True, yes=True)

    assert _git(worktree, "rev-parse", "HEAD") == moved


def test_a_detached_repo_is_put_back_on_its_base_ref(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path, spec="master")
    base = _git(worktree, "rev-parse", "refs/remotes/origin/master")
    _git(worktree, "switch", "-q", "-c", "scratch")
    _commit(worktree, "C")
    _git(worktree, "checkout", "-q", "--detach")
    _git(worktree, "branch", "-q", "-D", "scratch")

    cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert _git(worktree, "rev-parse", "HEAD") == base
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_a_worktree_on_another_branch_is_never_reset(tmp_path, capsys, xdg):
    """The config names featA; resetting whatever else is checked out would
    destroy work ow was never told about."""
    config, ws_dir, worktree = _workspace(tmp_path)
    _git(worktree, "switch", "-q", "-c", "hotfix")
    mine = _commit(worktree, "C")

    with pytest.raises(SystemExit) as exit_info:
        cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert exit_info.value.code == 1
    assert _git(worktree, "rev-parse", "HEAD") == mine
    err = capsys.readouterr().err
    assert "hotfix" in err
    assert "ow switch" in err


def test_a_repo_already_on_its_target_is_left_alone_without_asking(tmp_path, capsys, xdg, monkeypatch):
    _refuse_input(monkeypatch, "nothing to do means nothing to confirm")
    config, ws_dir, worktree = _workspace(tmp_path)
    before = _git(worktree, "rev-parse", "HEAD")

    cmd_reset(config, workspace=str(ws_dir))

    assert _git(worktree, "rev-parse", "HEAD") == before
    assert "already there" in capsys.readouterr().out


def test_declining_the_prompt_changes_nothing(tmp_path, capsys, xdg, monkeypatch):
    _answer(monkeypatch, "n")
    config, ws_dir, worktree = _workspace(tmp_path)
    mine = _commit(worktree, "C")

    with pytest.raises(SystemExit) as exit_info:
        cmd_reset(config, workspace=str(ws_dir))

    assert exit_info.value.code == 2
    assert _git(worktree, "rev-parse", "HEAD") == mine


def test_dry_run_shows_the_command_and_runs_nothing(tmp_path, capsys, xdg, monkeypatch):
    _refuse_input(monkeypatch, "a dry run must not prompt")
    config, ws_dir, worktree = _workspace(tmp_path)
    mine = _commit(worktree, "C")

    cmd_reset(config, workspace=str(ws_dir), hard=True, dry_run=True)

    assert _git(worktree, "rev-parse", "HEAD") == mine
    assert "git reset --hard origin/master" in capsys.readouterr().out


def test_commits_no_remote_carries_are_called_out(tmp_path, capsys, xdg):
    """The one number that says whether this is recoverable."""
    config, ws_dir, worktree = _workspace(tmp_path)
    _commit(worktree, "C")

    cmd_reset(config, workspace=str(ws_dir), dry_run=True)

    out = capsys.readouterr().out
    assert "drop 1 commit(s)" in out
    assert "on no remote" in out


def test_a_commit_a_remote_already_carries_raises_no_alarm(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    pushed = _commit(worktree, "C")
    _git(worktree, "update-ref", "refs/remotes/origin/featA", pushed)

    cmd_reset(config, workspace=str(ws_dir), dry_run=True)

    out = capsys.readouterr().out
    assert "drop 1 commit(s)" in out
    assert "on no remote" not in out


def test_only_narrows_the_work_to_one_repo(tmp_path, capsys, xdg):
    config, ws_dir, worktree = _workspace(tmp_path)
    other_bare = _bare_repo(tmp_path, "enterprise")
    other = ws_dir / "enterprise"
    _git(other_bare, "worktree", "add", "-q", str(other), "-b", "featA", "master")
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(
            repos={
                "community": parse_branch_spec("master..featA"),
                "enterprise": parse_branch_spec("master..featA"),
            },
        ),
    )
    _commit(worktree, "C")
    kept = _commit(other, "D")

    cmd_reset(config, workspace=str(ws_dir), only="community", yes=True)

    assert _git(other, "rev-parse", "HEAD") == kept
    assert "enterprise" not in capsys.readouterr().out


def _two_repo_workspace_with_addon(tmp_path: Path, addon: str = "my_addon") -> tuple[Config, Path, Path, Path]:
    """A core repo plus a non-core repo carrying one tracked addon, both
    worktrees sitting on featA at their unmoved base — moving nothing is
    the whole point of this fixture."""
    bare_c = _bare_repo(tmp_path, "community", odoo_core=True)
    bare_e = _bare_repo(tmp_path, "enterprise", addon=addon)
    ws_dir = tmp_path / "workspaces" / "test"
    ws_dir.mkdir(parents=True)
    wt_c = ws_dir / "community"
    wt_e = ws_dir / "enterprise"
    _git(bare_c, "worktree", "add", "-q", str(wt_c), "-b", "featA", "master")
    _git(bare_e, "worktree", "add", "-q", str(wt_e), "-b", "featA", "master")
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={
            "community": parse_branch_spec("master..featA"),
            "enterprise": parse_branch_spec("master..featA"),
        }),
    )
    return Config(remotes={}), ws_dir, wt_c, wt_e


def test_hard_reset_restores_a_deleted_tracked_addon_even_though_head_does_not_move(tmp_path, capsys, xdg):
    """The addon directory is deleted, uncommitted, from a non-core repo.
    Resetting to the ref the repo is already on moves nothing — HEAD stays
    exactly where it was — but the working tree comes back, and the
    refresh that follows must go by the plan that actually ran, not by
    comparing HEAD before and after."""
    config, ws_dir, wt_c, wt_e = _two_repo_workspace_with_addon(tmp_path)
    shutil.rmtree(wt_e / "my_addon")
    assert not (wt_e / "my_addon").exists()
    before_head = _git(wt_e, "rev-parse", "HEAD")

    cmd_reset(config, workspace=str(ws_dir), hard=True, yes=True)

    assert _git(wt_e, "rev-parse", "HEAD") == before_head
    assert (wt_e / "my_addon" / "__manifest__.py").exists()

    odoorc = configparser.ConfigParser(interpolation=None)
    odoorc.read(ws_dir / "odoorc")
    addon_paths = odoorc["options"]["addons_path"].split(",")
    assert "enterprise" in addon_paths
