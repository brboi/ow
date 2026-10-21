"""`ow repo add` and `ow repo ls` scenarios (§6, §4)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from ow.core.paths import repos_dir

BARE = "~/.local/share/ow/repos"


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; used where a failure is the expected state."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def test_add_outside_a_workspace_registers_the_bare(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    url = f"file://{remote}"

    result = lab.ow("repo", "add", "community", url)

    assert result.exit_code == 0
    bare = repos_dir() / "community.git"
    assert lab.git(bare, "remote", "get-url", "origin") == url
    assert lab.git(bare, "config", "core.logAllRefUpdates") == "true"
    assert lab.git(bare, "config", "remote.origin.promisor") == "true"
    assert lab.git(bare, "config", "remote.origin.partialclonefilter") == "blob:none"
    assert result.stdout.splitlines() == [
        f"git init --bare {BARE}/community.git",
        f"git -C {BARE}/community.git remote add origin {url}",
        f"git -C {BARE}/community.git config core.logAllRefUpdates true",
        f"git -C {BARE}/community.git ls-remote --symref origin HEAD",
        f"git -C {BARE}/community.git fetch --filter=blob:none origin 18.0",
        f"git -C {BARE}/community.git remote set-head origin 18.0",
    ]


def test_add_unknown_repo_without_a_url_is_a_usage_error(lab):
    result = lab.ow("repo", "add", "nope")

    assert result.exit_code == 2
    assert "error: unknown repo nope; pass a URL: ow repo add nope <url>" in result.stderr


def test_add_inside_a_workspace_detaches_and_records(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    url = f"file://{remote}"
    ws = lab.ws("voip")
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")

    result = lab.ow("repo", "add", "community", url, "--base", "18.0", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    bare = repos_dir() / "community.git"
    assert f"git -C {BARE}/community.git fetch origin 18.0" in lines
    assert (
        f"git -C {BARE}/community.git worktree add --detach ~/ws/voip/community origin/18.0"
        in lines
    )
    assert not _ok(ws / "community", "symbolic-ref", "-q", "HEAD")
    assert lab.sha(ws / "community", "HEAD") == lab.sha(bare, "origin/18.0")
    config = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert config["repos"] == {"community": "origin/18.0"}

    # A re-run only records the base: no git command runs at all.
    result = lab.ow("repo", "add", "community", "--base", "18.0", cwd=ws)

    assert result.exit_code == 0
    assert not [line for line in result.stdout.splitlines() if line.startswith("git ")]
    config = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert config["repos"] == {"community": "origin/18.0"}


def test_ls_lists_bares_and_marks_this_workspace(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    url = f"file://{remote}"
    assert lab.ow("repo", "add", "community", url).exit_code == 0

    result = lab.ow("repo", "ls")

    assert result.exit_code == 0
    assert result.stdout == f"community: {url}\n"

    ws = lab.ws("voip")
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")
    assert lab.ow("repo", "add", "community", url, "--base", "18.0", cwd=ws).exit_code == 0

    result = lab.ow("repo", "ls", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout == f"community: {url} (in this workspace)\n"
