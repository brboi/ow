"""Adopting a 2.x layout: a hand-built workspace `ow init` reads in place."""

from __future__ import annotations

import tomllib
from pathlib import Path

from ow.core.paths import repos_dir
from ow.core.plugins import load_plugins
from ow.core.session import open_session
from tests.conftest import git, git_ok, index_lines
from tests.lab import file_url

BARE = "~/.local/share/ow/repos"


def _two_x_bare(remote: Path, name: str) -> Path:
    """A bare as ow 2.x left it: `clone --bare --single-branch`, no fetch refspec nor `origin/HEAD`."""
    bare = repos_dir() / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(bare.parent, "clone", "--bare", "--single-branch", file_url(remote), str(bare))
    return bare


def test_adopt_a_two_x_layout(lab):
    remote = lab.remote("enterprise", {"18.0": {"README.md": "enterprise\n"}}, head="18.0")
    bare = _two_x_bare(remote, "enterprise")

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
    git(bare, "worktree", "add", str(worktree), "18.0")

    # Before `ow init`, an ordinary session already reads the layout and warns.
    session = open_session(str(root), need_workspace=False, plugins=load_plugins())
    warnings = "\n".join(session.warnings)
    assert "unknown setting version in .ow/config.toml" in warnings
    assert "`ow config unset version`" in warnings
    assert "x..y" in warnings
    assert "run `ow init`" in warnings

    head_before = git(worktree, "rev-parse", "HEAD")

    result = lab.ow("init", "web", cwd=lab.home)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    # The 2.x bare is repaired with explicit-refspec fetches only; origin/HEAD infers the default base.
    assert f"git -C {BARE}/enterprise.git config remote.origin.tagOpt --no-tags" in lines
    assert f"git -C {BARE}/enterprise.git config core.logAllRefUpdates true" in lines
    assert f"git -C {BARE}/enterprise.git ls-remote --symref origin HEAD" in lines
    assert (
        f"git -C {BARE}/enterprise.git fetch --filter=blob:none "
        "origin +refs/heads/18.0:refs/remotes/origin/18.0"
    ) in lines
    assert f"git -C {BARE}/enterprise.git remote set-head origin 18.0" in lines
    assert "# enterprise: default base origin/18.0 (origin/HEAD)" in lines
    # Adopting never moves the worktree.
    assert git(worktree, "rev-parse", "HEAD") == head_before

    assert "dropped version = 2" in result.stderr
    assert 'dropped templates = "legacy"' in result.stderr
    assert 'dropped vars.foo = "bar"' in result.stderr
    assert 'dropped repos.community = "x..y"' in result.stderr

    config = tomllib.loads((root / ".ow" / "config.toml").read_text())
    assert set(config) == {"repos"}
    assert config["repos"] == {"enterprise": "origin/18.0"}
    assert str(Path(root).resolve()) in index_lines()

    adopted = open_session(str(root), need_workspace=False, plugins=load_plugins())
    assert [repo.name for repo in adopted.workspace.repos] == ["enterprise"]
    assert adopted.workspace.repos[0].path == Path(worktree).resolve()
    assert adopted.workspace.repos[0].base == "origin/18.0"


def test_pull_on_an_adopted_two_x_bare_moves_the_worktree(lab):
    remote = lab.remote("enterprise", {"18.0": {"README.md": "one\n"}}, head="18.0")
    _two_x_bare(remote, "enterprise")

    root = lab.home / "web"
    worktree = root / "web"
    (root / ".ow").mkdir(parents=True)
    (root / ".ow" / "config.toml").write_text("")
    git(repos_dir() / "enterprise.git", "worktree", "add", str(worktree), "18.0")

    assert lab.ow("init", "web", cwd=lab.home).exit_code == 0

    collaborator = lab.collaborator("upstream", of=remote)
    collaborator.commit({"README.md": "one\ntwo\n"}, message="two")
    collaborator.push("18.0")

    result = lab.ow("pull", cwd=root)

    assert result.exit_code == 0
    assert (worktree / "README.md").read_text() == "one\ntwo\n"


def test_a_two_x_bare_is_repaired_before_its_worktree_is_added(lab):
    """`ow init <dir> <name>` on an existing 2.x bare repairs it before the add.

    The explicit-refspec fetch that precedes `worktree add` writes
    `refs/remotes/origin/18.0`; `origin/HEAD` is what infers the default base.
    """
    remote = lab.remote("enterprise", {"18.0": {"README.md": "enterprise\n"}}, head="18.0")
    bare = _two_x_bare(remote, "enterprise")
    assert not git_ok(bare, "config", "--get", "remote.origin.fetch")

    ws = lab.ws("voip")
    result = lab.ow("init", str(ws), "enterprise")

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert f"git -C {BARE}/enterprise.git config remote.origin.tagOpt --no-tags" in lines
    assert f"git -C {BARE}/enterprise.git config core.logAllRefUpdates true" in lines
    assert f"git -C {BARE}/enterprise.git ls-remote --symref origin HEAD" in lines
    assert (
        f"git -C {BARE}/enterprise.git fetch --filter=blob:none "
        "origin +refs/heads/18.0:refs/remotes/origin/18.0"
    ) in lines
    assert f"git -C {BARE}/enterprise.git remote set-head origin 18.0" in lines
    assert (
        f"git -C {BARE}/enterprise.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0"
        in lines
    )
    assert "# enterprise: default base origin/18.0 (origin/HEAD)" in lines
    assert "added enterprise (base origin/18.0)" in lines
    assert (ws / "enterprise" / "README.md").read_text() == "enterprise\n"
    # The add is detached, so the new worktree sits exactly on the repaired ref.
    assert git(ws / "enterprise", "rev-parse", "HEAD") == git(bare, "rev-parse", "origin/18.0")
    config = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert config["repos"] == {"enterprise": "origin/18.0"}


def test_a_wide_fetch_refspec_is_unset_and_other_remotes_stay(lab):
    """A dev bare with the wide refspec loses it; only origin is touched."""
    remote = lab.remote("enterprise", {"18.0": {"README.md": "enterprise\n"}}, head="18.0")
    bare = _two_x_bare(remote, "enterprise")
    git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(bare, "remote", "add", "mirror", file_url(remote))

    result = lab.ow("init", str(lab.ws("voip")), "enterprise")

    assert result.exit_code == 0
    assert (
        f"git -C {BARE}/enterprise.git config --unset-all remote.origin.fetch"
        in result.stdout.splitlines()
    )
    assert not git_ok(bare, "config", "--get", "remote.origin.fetch")
    assert "mirror" in git(bare, "remote").split()
