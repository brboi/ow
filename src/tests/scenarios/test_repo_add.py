"""`ow repo add` and `ow repo ls` scenarios (§6, §4)."""

from __future__ import annotations

import tomllib
from ow.core.paths import repos_dir
from tests.conftest import git_ok
from tests.lab import file_url

BARE = "~/.local/share/ow/repos"


def test_add_outside_a_workspace_registers_the_bare(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    url = file_url(remote)

    result = lab.ow("repo", "add", "community", url)

    assert result.exit_code == 0
    bare = repos_dir() / "community.git"
    assert lab.git(bare, "remote", "get-url", "origin") == url
    assert lab.git(bare, "config", "core.logAllRefUpdates") == "true"
    assert lab.git(bare, "config", "remote.origin.promisor") == "true"
    assert lab.git(bare, "config", "remote.origin.partialclonefilter") == "blob:none"
    assert result.stdout.splitlines() == [
        f"git init --bare {BARE}/community.git",
        f"git -C {BARE}/community.git config remote.origin.url {url}",
        f"git -C {BARE}/community.git config remote.origin.tagOpt --no-tags",
        f"git -C {BARE}/community.git config core.logAllRefUpdates true",
        f"git -C {BARE}/community.git ls-remote --symref origin HEAD",
        f"git -C {BARE}/community.git fetch --filter=blob:none "
        "origin +refs/heads/18.0:refs/remotes/origin/18.0",
        f"git -C {BARE}/community.git remote set-head origin 18.0",
    ]


def test_a_new_bare_carries_no_fetch_refspec(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", file_url(remote)).exit_code == 0
    bare = repos_dir() / "community.git"

    assert not git_ok(bare, "config", "--get", "remote.origin.fetch")
    assert lab.git(bare, "config", "remote.origin.tagOpt") == "--no-tags"
    assert git_ok(bare, "rev-parse", "--verify", "refs/remotes/origin/HEAD")


def test_a_bare_fetch_creates_no_branch_ow_does_not_track(lab):
    remote = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", file_url(remote)).exit_code == 0
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"x.py": "x\n"}, message="x", branch="other")
    collaborator.push("other")
    bare = repos_dir() / "community.git"

    lab.git(bare, "fetch", "origin")

    assert not git_ok(bare, "rev-parse", "--verify", "refs/remotes/origin/other")
    assert git_ok(bare, "rev-parse", "--verify", "refs/remotes/origin/18.0")


def test_add_unknown_repo_without_a_url_is_a_usage_error(lab):
    result = lab.ow("repo", "add", "nope")

    assert result.exit_code == 2
    assert "error: unknown repo nope; pass a URL: ow repo add nope <url>" in result.stderr


def test_add_inside_a_workspace_detaches_and_records(lab):
    remote = lab.remote("community", {"18.0": {"README.md": "c"}}, head="18.0")
    url = file_url(remote)
    ws = lab.ws("voip")
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")

    result = lab.ow("repo", "add", "community", url, "--base", "18.0", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    bare = repos_dir() / "community.git"
    assert (
        f"git -C {BARE}/community.git fetch origin "
        f"+refs/heads/18.0:refs/remotes/origin/18.0"
    ) in lines
    assert (
        f"git -C {BARE}/community.git worktree add --detach ~/ws/voip/community origin/18.0"
        in lines
    )
    assert not git_ok(ws / "community", "symbolic-ref", "-q", "HEAD")
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
    url = file_url(remote)
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


def test_a_failed_bare_creation_leaves_nothing_behind(lab):
    bare = repos_dir() / "custom.git"

    bad = lab.ow("repo", "add", "custom", "file:///nonexistent")

    assert bad.exit_code == 1
    assert not bare.exists()

    remote = lab.remote("custom", {"18.0": {"README.md": "c"}}, head="18.0")
    good = lab.ow("repo", "add", "custom", file_url(remote))

    assert good.exit_code == 0
    assert lab.git(bare, "remote", "get-url", "origin") == file_url(remote)


def test_a_bad_base_is_refused_before_the_bare_is_created(lab):
    remote = lab.remote("custom", {"18.0": {"README.md": "c"}}, head="18.0")
    ws = lab.ws("voip")
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")

    result = lab.ow("repo", "add", "custom", file_url(remote), "--base", "deadbeef", cwd=ws)

    assert result.exit_code == 2
    assert "base cannot be a SHA" in result.stderr
    assert not (repos_dir() / "custom.git").exists()


def test_a_registered_bare_needs_no_url(lab):
    remote = lab.remote("custom", {"18.0": {"README.md": "c"}}, head="18.0")
    assert lab.ow("repo", "add", "custom", file_url(remote)).exit_code == 0
    ws = lab.ws("voip")
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config.toml").write_text("")

    result = lab.ow("repo", "add", "custom", "--base", "18.0", cwd=ws)

    assert result.exit_code == 0
    assert (ws / "custom").is_dir()
    config = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert config["repos"] == {"custom": "origin/18.0"}
