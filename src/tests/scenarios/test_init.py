"""`ow init` scenarios (§6, §13): first-time flow, the `--branch` cases, repair."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import add_repo, git_ok, index_lines

BARE = "~/.local/share/ow/repos"


def test_first_time_flow(lab):
    add_repo(lab)
    add_repo(lab, "enterprise", {"18.0": {"README.md": "enterprise\n"}})
    ws = lab.ws("voip")

    result = lab.ow(
        "init", str(ws), "community", "enterprise", "--base", "18.0", "--branch", "18.0-voip-boi"
    )

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    for name in ("community", "enterprise"):
        assert (
            f"git -C {BARE}/{name}.git fetch origin "
            f"+refs/heads/18.0:refs/remotes/origin/18.0"
        ) in lines
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
        assert not git_ok(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        assert f"added {name} (base origin/18.0)" in lines

    assert str(Path(ws).resolve()) in index_lines()
    config = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert config["repos"] == {"community": "origin/18.0", "enterprise": "origin/18.0"}
    assert (ws / "mise" / "conf.d" / "00-ow.toml").is_file()
    assert (ws / ".vscode" / "launch.json").is_file()


def test_branch_local_case(lab):
    remote = add_repo(lab)
    bare = repos_dir() / "community.git"
    lab.git(bare, "branch", "b", lab.sha(remote, "18.0"))
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    assert f"git -C {BARE}/community.git worktree add ~/ws/voip/community b" in result.stdout.splitlines()
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == "b"
    assert not git_ok(bare, "config", "--get", "branch.b.ow-base")


def test_branch_on_one_remote_case(lab):
    remote = add_repo(lab)
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"feature.py": "# feature\n"}, message="feature", branch="b")
    collaborator.push("b")
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert (
        f"git -C {BARE}/community.git fetch origin +refs/heads/b:refs/remotes/origin/b" in lines
    )
    assert (
        f"git -C {BARE}/community.git worktree add --no-track -b b ~/ws/voip/community origin/b"
        in lines
    )
    assert f"git -C community config branch.b.remote origin" in lines
    assert f"git -C community config branch.b.merge refs/heads/b" in lines
    assert f"git -C community config branch.b.ow-base origin/18.0" in lines
    assert lab.git(repos_dir() / "community.git", "config", "--get", "branch.b.ow-base") == "origin/18.0"
    assert lab.git(repos_dir() / "community.git", "config", "--get", "branch.b.remote") == "origin"
    assert lab.git(repos_dir() / "community.git", "config", "--get", "branch.b.merge") == "refs/heads/b"


def test_branch_neither_remote_case(lab):
    add_repo(lab)
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert (
        f"git -C {BARE}/community.git worktree add --no-track -b b ~/ws/voip/community origin/18.0"
        in lines
    )
    assert f"git -C community config branch.b.ow-base origin/18.0" in lines
    assert not git_ok(ws / "community", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")


def test_a_repo_already_present_is_listed_with_switch(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "--base", "18.0").exit_code == 0

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    assert "# community: present; run `ow switch b`" in result.stdout.splitlines()


def test_repair_after_moving_the_workspace(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
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
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
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


def test_a_typo_in_a_later_name_clones_nothing(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "nope")

    assert result.exit_code == 2
    assert "error: unknown repo nope" in result.stderr
    assert not ws.exists()


def test_a_repeated_name_is_added_once(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "community", "--base", "18.0")

    assert result.exit_code == 0
    adds = [line for line in result.stdout.splitlines() if "worktree add" in line]
    assert len(adds) == 1


def test_a_missing_worktree_is_recreated(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "--base", "18.0").exit_code == 0
    shutil.rmtree(ws / "community")

    result = lab.ow("init", str(ws))

    assert result.exit_code == 0
    assert (ws / "community").is_dir()
    assert lab.git(ws / "community", "rev-parse", "--verify", "HEAD")


def test_no_switch_hint_when_already_on_the_branch(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c"}})
    ws = lab.ws("voip")
    first = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")
    assert first.exit_code == 0
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == "b"

    result = lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", "b")

    assert result.exit_code == 0
    assert "# community: present; run `ow switch b`" not in result.stdout
