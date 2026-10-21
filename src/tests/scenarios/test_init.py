"""`ow init` scenarios (§6, §13): first-time flow, the `--branch` cases, repair."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

from ow.core.paths import repos_dir

BARE = "~/.local/share/ow/repos"


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; a failure is a state under test."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def _index_lines() -> list[str]:
    """The workspace index file's entries."""
    index_file = Path(os.environ["XDG_STATE_HOME"]) / "ow" / "workspaces"
    if not index_file.exists():
        return []
    return [line for line in index_file.read_text().splitlines() if line]


def test_first_time_flow(lab):
    community = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    enterprise = lab.remote("enterprise", {"18.0": {"README.md": "enterprise\n"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{community}").exit_code == 0
    assert lab.ow("repo", "add", "enterprise", f"file://{enterprise}").exit_code == 0
    ws = lab.ws("voip")

    result = lab.ow(
        "init", str(ws), "community", "enterprise", "--base", "18.0", "--branch", "18.0-voip-boi"
    )

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    for name in ("community", "enterprise"):
        assert f"git -C {BARE}/{name}.git fetch origin 18.0" in lines
        assert (
            f"git -C {BARE}/{name}.git worktree add --no-track -b 18.0-voip-boi "
            f"~/ws/voip/{name} origin/18.0"
        ) in lines
        assert (
            f"git -C {name} config branch.18.0-voip-boi.ow-base origin/18.0" in lines
        )
        worktree = ws / name
        assert lab.git(worktree, "symbolic-ref", "--short", "HEAD") == "18.0-voip-boi"
        assert (
            lab.git(repos_dir() / f"{name}.git", "config", "--get", "branch.18.0-voip-boi.ow-base")
            == "origin/18.0"
        )
        assert not _ok(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        assert f"added {name} (base origin/18.0)" in lines

    assert str(Path(ws).resolve()) in _index_lines()
    config = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert config["repos"] == {"community": "origin/18.0", "enterprise": "origin/18.0"}
    assert (ws / "mise" / "conf.d" / "00-ow.toml").is_file()
    assert (ws / ".vscode" / "launch.json").is_file()


def test_branch_local_case(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    bare = repos_dir() / "community.git"
    lab.git(bare, "branch", "b", lab.sha(remote, "18.0"))
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    assert f"git -C {BARE}/community.git worktree add ~/ws/voip/community b" in result.stdout.splitlines()
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == "b"
    assert not _ok(bare, "config", "--get", "branch.b.ow-base")


def test_branch_on_one_remote_case(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"feature.py": "# feature\n"}, message="feature", branch="b")
    collaborator.push("b")
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert f"git -C {BARE}/community.git fetch origin b" in lines
    assert (
        f"git -C {BARE}/community.git worktree add --track -b b ~/ws/voip/community origin/b"
        in lines
    )
    assert f"git -C community config branch.b.ow-base origin/18.0" in lines
    assert lab.git(repos_dir() / "community.git", "config", "--get", "branch.b.ow-base") == "origin/18.0"
    assert lab.git(ws / "community", "rev-parse", "--abbrev-ref", "@{u}") == "origin/b"


def test_branch_neither_remote_case(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert (
        f"git -C {BARE}/community.git worktree add --no-track -b b ~/ws/voip/community origin/18.0"
        in lines
    )
    assert f"git -C community config branch.b.ow-base origin/18.0" in lines
    assert not _ok(ws / "community", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")


def test_a_repo_already_present_is_listed_with_switch(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "--base", "18.0").exit_code == 0

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    assert "# community: present; run `ow switch b`" in result.stdout.splitlines()


def test_repair_after_moving_the_workspace(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "--base", "18.0").exit_code == 0
    moved = lab.home / "archive" / "voip"
    moved.parent.mkdir(parents=True)
    ws.rename(moved)

    result = lab.ow("init", str(moved))

    assert result.exit_code == 0
    assert "git -C community worktree repair" in result.stdout.splitlines()
    listing = lab.git(repos_dir() / "community.git", "worktree", "list", "--porcelain")
    assert f"worktree {(moved / 'community').resolve()}" in listing.splitlines()


def test_a_pruned_worktree_is_forgotten_and_fails(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "--base", "18.0").exit_code == 0
    moved = lab.home / "archive" / "voip"
    moved.parent.mkdir(parents=True)
    ws.rename(moved)
    lab.git(repos_dir() / "community.git", "worktree", "prune")

    result = lab.ow("init", str(moved))

    assert result.exit_code == 1
    assert "community: worktree forgotten by" in result.stderr
    assert "move the directory away and re-run `ow init`" in result.stderr
