"""Adopting a 2.x layout (§13): a hand-built workspace `ow init` reads in place."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

from ow.core.paths import repos_dir
from ow.core.plugins import load_plugins
from ow.core.session import open_session


def _git(path: Path, *args: str) -> str:
    """Run git in `path` and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _index_lines() -> list[str]:
    """The workspace index file's entries."""
    index_file = Path(os.environ["XDG_STATE_HOME"]) / "ow" / "workspaces"
    if not index_file.exists():
        return []
    return [line for line in index_file.read_text().splitlines() if line]


def test_adopt_a_two_x_layout(lab):
    remote = lab.remote("enterprise", {"18.0": {"README.md": "enterprise\n"}}, head="18.0")
    bare = repos_dir() / "enterprise.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(bare.parent, "init", "--bare", str(bare))
    _git(bare, "remote", "add", "origin", f"file://{remote}")
    _git(bare, "fetch", "origin", "18.0")
    _git(bare, "remote", "set-head", "origin", "18.0")
    _git(bare, "branch", "18.0", "origin/18.0")

    root = lab.home / "web"
    worktree = root / "web"
    (root / ".ow").mkdir(parents=True)
    (root / ".ow" / "config.toml").write_text(
        "version = 2\n"
        'templates = "legacy"\n'
        "\n[vars]\n"
        'foo = "bar"\n'
        "\n[repos]\n"
        'community = "x..y"\n'
    )
    _git(bare, "worktree", "add", str(worktree), "18.0")

    # Before `ow init`, an ordinary session already reads the layout and warns (§13).
    session = open_session(str(root), need_workspace=False, plugins=load_plugins())
    warnings = "\n".join(session.warnings)
    assert "unknown setting version in .ow/config.toml" in warnings
    assert "`ow config unset version`" in warnings
    assert "x..y" in warnings
    assert "run `ow init`" in warnings

    refs_before = _git(bare, "for-each-ref", "--format=%(refname) %(objectname)")
    head_before = _git(worktree, "rev-parse", "HEAD")

    result = lab.ow("init", "web", cwd=lab.home)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert "# enterprise: default base origin/18.0 (origin/HEAD)" in lines
    assert not [line for line in lines if line.startswith("git ")]

    assert "dropped version = 2" in result.stderr
    assert 'dropped templates = "legacy"' in result.stderr
    assert 'dropped vars.foo = "bar"' in result.stderr
    assert 'dropped repos.community = "x..y"' in result.stderr

    # No git command ran: the refs and the worktree's HEAD are untouched.
    assert _git(bare, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert _git(worktree, "rev-parse", "HEAD") == head_before

    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert set(config) == {"repos"}
    assert config["repos"] == {"enterprise": "origin/18.0"}
    assert str(Path(root).resolve()) in _index_lines()

    adopted = open_session(str(root), need_workspace=False, plugins=load_plugins())
    assert [repo.name for repo in adopted.workspace.repos] == ["enterprise"]
    assert adopted.workspace.repos[0].path == Path(worktree).resolve()
    assert adopted.workspace.repos[0].base == "origin/18.0"
