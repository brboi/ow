"""`ow` top-level CLI behaviour: help, --version, exit codes, --only validation.

The workspace here is built with plain git and `write_workspace`, never with
`ow init`, so these scenarios hold while the command modules land wave by wave.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import repos_dir

REPO = "community"
BASE = "18.0"


def workspace(lab, *, name: str = "voip") -> Path:
    """A one-repo workspace: a bare in repos_dir(), a worktree, a config, an index entry."""
    origin = lab.remote(REPO, {BASE: {"README.md": "hi\n"}}, head=BASE)
    bare = repos_dir() / f"{REPO}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", str(origin))
    lab.git(bare, "fetch", "origin")

    root = lab.ws(name)
    root.mkdir(parents=True, exist_ok=True)
    lab.git(bare, "worktree", "add", "--detach", str(root / REPO), f"origin/{BASE}")

    write_workspace(root / ".ow" / "config.toml", {REPO: f"origin/{BASE}"}, {}, declared={})
    index_add(root)
    return root


def test_bare_ow_prints_help_and_exits_zero(lab):
    result = lab.ow()

    assert result.exit_code == 0
    assert "Ready-to-code Odoo workspaces on git worktrees" in result.stdout
    # Every §4 command is wired up front, whatever wave wrote its module.
    for command in (
        "init", "status", "fetch", "switch", "pull", "rebase", "reset",
        "refresh", "files", "ls", "cd", "shell-init", "rm", "prune",
        "repo", "config",
    ):
        assert command in result.stdout


def test_version_exits_zero(lab):
    result = lab.ow("--version")

    assert result.exit_code == 0
    assert result.stdout.startswith("ow ")
    assert result.stderr == ""


def test_unknown_command_exits_two(lab):
    result = lab.ow("nope")

    assert result.exit_code == 2
    assert "No such command 'nope'" in result.stderr


def test_only_with_an_unknown_repo_names_the_valid_ones(lab):
    root = workspace(lab)

    result = lab.ow("status", "--only", "bogus", cwd=root)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "bogus" in result.stderr
    assert REPO in result.stderr


def test_cd_outside_a_workspace_exits_two(lab):
    result = lab.ow("cd", cwd=lab.home)

    assert result.exit_code == 2
    assert "not inside an ow workspace" in result.stderr
