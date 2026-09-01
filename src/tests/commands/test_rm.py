import subprocess
from pathlib import Path

import pytest

from ow.commands.rm import cmd_rm, RepoRemoval, _display_summary
from ow.utils import index, paths
from ow.utils.config import BranchSpec, WorkspaceConfig, write_workspace_config
from ow.utils.git import dirty_files

MARKER = Path(".ow") / "config.toml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "master"], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "T")


def _bare_repo(tmp_path: Path, alias: str = "community") -> Path:
    """A real bare repo where ow keeps them, mirroring what `ow init` leaves behind."""
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    _git_init(src)
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


def _branches(bare: Path) -> list[str]:
    out = _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return out.splitlines() if out else []


def _registered_worktrees(bare: Path) -> list[str]:
    out = _git(bare, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _make_workspace(
    tmp_path: Path, name: str, repos: dict[str, BranchSpec],
    *, bare_repos: dict[str, Path] | None = None,
) -> Path:
    """Create a workspace dir with .ow/config.toml, worktrees, and index entry.

    If bare_repos is given, worktrees are created from those bare repos.
    """
    ws = tmp_path / "workspaces" / name
    ws.mkdir(parents=True)
    (ws / ".ow").mkdir()
    ws_cfg = WorkspaceConfig(repos=repos, templates=["common"])
    write_workspace_config(ws / MARKER, ws_cfg)

    if bare_repos:
        for alias, spec in repos.items():
            bare = bare_repos[alias]
            wt_path = ws / alias
            if spec.is_detached:
                _git(bare, "worktree", "add", "--detach", str(wt_path), spec.base_ref)
            else:
                _git(bare, "worktree", "add", "-b", spec.local_branch, str(wt_path), spec.base_ref)

    index.remember(ws)
    return ws


def _refuse_input(monkeypatch, reason: str = "rm must not prompt here"):
    def _boom(prompt: str = "") -> str:
        raise AssertionError(reason)
    monkeypatch.setattr("builtins.input", _boom)


def _answer(monkeypatch, reply: str):
    monkeypatch.setattr("builtins.input", lambda prompt="": reply)


# ---------------------------------------------------------------------------
# Resolution failures
# ---------------------------------------------------------------------------

def test_rm_unknown_name_exits_nonzero(tmp_path, capsys, xdg):
    with pytest.raises(SystemExit) as exc:
        cmd_rm("nonexistent")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "ow ls" in err


def test_rm_multiple_matches_exits_nonzero(tmp_path, capsys, xdg):
    ws1 = _make_workspace(tmp_path, "dupe", {})
    # A second workspace with the same name in a different parent.
    ws2 = tmp_path / "other" / "dupe"
    ws2.mkdir(parents=True)
    (ws2 / ".ow").mkdir()
    write_workspace_config(ws2 / MARKER, WorkspaceConfig(repos={}, templates=["common"]))
    index.remember(ws2)

    with pytest.raises(SystemExit) as exc:
        cmd_rm("dupe")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Multiple" in err
    assert str(ws1) in err
    assert str(ws2) in err


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

def test_rm_asks_before_removing_and_no_changes_nothing(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "n")
    with pytest.raises(SystemExit) as exc:
        cmd_rm("canary")
    assert exc.value.code == 2
    assert "Aborted." in capsys.readouterr().out

    # Nothing changed.
    assert ws.exists()
    assert "master-canary" in _branches(bare)
    assert str(ws / "community") in _registered_worktrees(bare)
    assert ws in index.list_workspaces()


def test_rm_eof_means_no(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _refuse_input(monkeypatch, "EOF should mean no, not a crash")
    # input() raises EOFError, but our monkeypatch raises AssertionError.
    # Test the real EOFError path instead:
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    with pytest.raises(SystemExit) as exc:
        cmd_rm("canary")
    assert exc.value.code == 2


def test_rm_yes_skips_prompt(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _refuse_input(monkeypatch, "yes=True must not prompt")
    cmd_rm("canary", yes=True)

    assert not ws.exists()
    assert "master-canary" not in _branches(bare)
    assert ws not in index.list_workspaces()


def test_rm_proceeds_when_answer_is_yes(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    assert not ws.exists()
    assert "master-canary" not in _branches(bare)
    assert str(ws) not in _registered_worktrees(bare)
    assert ws not in index.list_workspaces()


# ---------------------------------------------------------------------------
# Cleanup: worktrees, branches, directory, index
# ---------------------------------------------------------------------------

def test_rm_removes_workspace_directory(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    # Put some extra files in the workspace dir to prove rmtree gets everything.
    (ws / "mise.toml").write_text("# mise")
    (ws / ".data").mkdir()

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    assert not ws.exists()


def test_rm_unregisters_worktree_from_bare_repo(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    assert str(ws / "community") not in _registered_worktrees(bare)


def test_rm_deletes_local_branch(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    assert "master-canary" in _branches(bare)

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    assert "master-canary" not in _branches(bare)


def test_rm_keeps_bare_repo(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    # Bare repos are shared — they stay.
    assert bare.exists()
    assert "master" in _branches(bare)


def test_rm_drops_index_entry(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    before = paths.index_file().read_text()
    assert str(ws.resolve()) in before

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    after = paths.index_file().read_text()
    assert str(ws.resolve()) not in after


# ---------------------------------------------------------------------------
# Detached specs
# ---------------------------------------------------------------------------

def test_rm_detached_does_not_delete_branch(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    # Create a branch so we can prove rm doesn't touch it in detached mode.
    _git(bare, "branch", "pre-existing")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    # The worktree is gone, but the bare repo's branches are untouched.
    assert str(ws / "community") not in _registered_worktrees(bare)
    assert "master" in _branches(bare)
    assert "pre-existing" in _branches(bare)


def test_rm_detached_shows_no_branch_warning_in_summary(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "n")
    with pytest.raises(SystemExit):
        cmd_rm("canary")

    out = capsys.readouterr().out
    assert "detached" in out


# ---------------------------------------------------------------------------
# Warnings: unpushed commits and uncommitted changes
# ---------------------------------------------------------------------------

def test_rm_warns_about_unpushed_commits(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    # Add a commit on the branch that no remote has.
    wt = ws / "community"
    (wt / "new.txt").write_text("new")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "unpushed work")

    _answer(monkeypatch, "n")
    with pytest.raises(SystemExit):
        cmd_rm("canary")

    out = capsys.readouterr().out
    assert "unpushed" in out
    assert "1 unpushed commit" in out


def test_rm_warns_about_dirty_files(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    # Make a dirty change in the worktree.
    (ws / "community" / "a.txt").write_text("modified")

    _answer(monkeypatch, "n")
    with pytest.raises(SystemExit):
        cmd_rm("canary")

    out = capsys.readouterr().out
    assert "uncommitted" in out
    assert "a.txt" in out


def test_rm_does_not_warn_when_branch_is_pushed(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    # Push the branch to a remote ref so is_branch_pushed returns True.
    _git(bare, "update-ref", "refs/remotes/origin/master-canary", "refs/heads/master-canary")

    _answer(monkeypatch, "n")
    with pytest.raises(SystemExit):
        cmd_rm("canary")

    out = capsys.readouterr().out
    assert "unpushed" not in out
    assert "safe to delete" in out


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_rm_tolerates_missing_bare_repo(tmp_path, capsys, xdg, monkeypatch):
    """A workspace whose bare repo is gone must still have its directory and index cleaned up."""
    ws = _make_workspace(
        tmp_path, "orphan",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos=None,  # no bare repo created at all
    )
    # Create the worktree dir manually so there's something to clean up.
    (ws / "community").mkdir()

    _answer(monkeypatch, "y")
    cmd_rm("orphan")

    assert not ws.exists()
    assert ws not in index.list_workspaces()


def test_rm_says_done_when_finished(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    assert "Done." in capsys.readouterr().out


def test_rm_multiple_repos(tmp_path, capsys, xdg, monkeypatch):
    bare_c = _bare_repo(tmp_path, "community")
    bare_e = _bare_repo(tmp_path, "enterprise")
    ws = _make_workspace(
        tmp_path, "canary",
        {
            "community": BranchSpec("origin/master", "master-canary"),
            "enterprise": BranchSpec("origin/master", "master-canary"),
        },
        bare_repos={"community": bare_c, "enterprise": bare_e},
    )

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    assert not ws.exists()
    assert "master-canary" not in _branches(bare_c)
    assert "master-canary" not in _branches(bare_e)
    assert "master" in _branches(bare_c)
    assert "master" in _branches(bare_e)
    assert ws not in index.list_workspaces()


def test_rm_does_not_emit_fatal_when_worktree_not_registered(tmp_path, capsys, xdg, monkeypatch):
    """A workspace dir whose repo subdirectory exists on disk but isn't a
    registered worktree in the bare repo must not produce a git fatal error.
    Reproduces the real-world bug: `git worktree remove` on an unregistered
    path prints 'fatal: ... is not a working tree' to stderr.
    """
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master")},
        bare_repos=None,  # no worktree registered — just a plain directory
    )
    # Create the directory so it looks like a worktree on disk but isn't one.
    (ws / "community").mkdir()
    (ws / "community" / "a.txt").write_text("a")

    _answer(monkeypatch, "y")
    cmd_rm("canary")

    err = capsys.readouterr().err
    assert "fatal" not in err.lower()
    assert "not a working tree" not in err.lower()
    assert not ws.exists()
    assert ws not in index.list_workspaces()


# ---------------------------------------------------------------------------
# #33: a backup of the removed workspace's config is saved for `ow init -c`
# ---------------------------------------------------------------------------

def test_rm_saves_a_config_backup(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    _answer(monkeypatch, "y")

    capsys.readouterr()
    cmd_rm("canary")

    out = capsys.readouterr().out
    backups = sorted(paths.backups_dir().glob("canary-*.toml"))
    assert len(backups) == 1
    # It really is a restorable workspace config.
    from ow.utils.config import load_workspace_config

    saved = load_workspace_config(backups[0])
    assert "community" in saved.repos
    # And the command told the user where it is and how to restore.
    assert str(backups[0]) in out
    assert "ow init canary -c" in out


def test_rm_without_a_backup_dir_does_not_crash(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    _answer(monkeypatch, "y")

    # Remove the backup directory to prove the mkdir path is exercised.
    if paths.backups_dir().exists():
        import shutil as _shutil
        _shutil.rmtree(paths.backups_dir())

    cmd_rm("canary")

    assert sorted(paths.backups_dir().glob("canary-*.toml"))
    assert not ws.exists()


def test_rm_backup_failure_does_not_block_removal(tmp_path, capsys, xdg, monkeypatch):
    bare = _bare_repo(tmp_path, "community")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": BranchSpec("origin/master", "master-canary")},
        bare_repos={"community": bare},
    )
    _answer(monkeypatch, "y")

    monkeypatch.setattr("ow.commands.rm.shutil.copy2", lambda *a, **kw: (_ for _ in ()).throw(OSError("no space")))

    capsys.readouterr()
    cmd_rm("canary")

    err = capsys.readouterr().err
    assert "could not save config backup" in err
    assert not ws.exists()


# ---------------------------------------------------------------------------
# _delete_branch return value
# ---------------------------------------------------------------------------

def test_delete_branch_returns_true_on_success(tmp_path, xdg, monkeypatch):
    """_delete_branch returns True when git branch -D succeeds."""
    from ow.commands.rm import _delete_branch
    bare = _bare_repo(tmp_path, "community")
    # Create a branch to delete
    _git(bare, "branch", "to-delete")
    assert "to-delete" in _branches(bare)
    result = _delete_branch(bare, "to-delete", "community")
    assert result is True
    assert "to-delete" not in _branches(bare)


def test_delete_branch_returns_false_on_failure(tmp_path, xdg, monkeypatch):
    """_delete_branch returns False when git branch -D fails."""
    from ow.commands.rm import _delete_branch
    bare = _bare_repo(tmp_path, "community")
    # Try to delete a branch that doesn't exist
    result = _delete_branch(bare, "nonexistent", "community")
    assert result is False

def test_rm_no_branch_warning_when_delete_succeeds(tmp_path, capsys, xdg, monkeypatch):
    """The 'could not delete branch' warning only fires on real failure."""
    bare = _bare_repo(tmp_path, "community")
    spec = BranchSpec("origin/master", "my-feature")
    ws = _make_workspace(
        tmp_path, "canary",
        {"community": spec},
        bare_repos={"community": bare},
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    cmd_rm("canary")
    out = capsys.readouterr()
    assert "could not delete branch" not in out.err
    assert "could not delete branch" not in out.out
