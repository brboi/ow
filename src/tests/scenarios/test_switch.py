"""`ow switch` scenarios (§6): the enterprise-only branch, `-c --base`, the
all-or-nothing refusal, the split after a failure and its idempotent re-run."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ow.core.paths import repos_dir

BRANCH = "18.0-voip"


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; a failure is a state under test."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def _workspace(lab, *names: str) -> Path:
    """A workspace at ~/ws/voip: one registered remote and one detached worktree per name."""
    for name in names:
        remote = lab.remote(name, {"18.0": {"README.md": f"{name}\n"}}, head="18.0")
        assert lab.ow("repo", "add", name, f"file://{remote}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), *names, "--base", "18.0").exit_code == 0
    return ws


def test_branch_only_in_enterprise_switches_it_and_lists_community(lab):
    community = lab.remote("community", {"18.0": {"README.md": "c\n"}}, head="18.0")
    enterprise = lab.remote("enterprise", {"18.0": {"README.md": "e\n"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{community}").exit_code == 0
    assert lab.ow("repo", "add", "enterprise", f"file://{enterprise}").exit_code == 0
    dev = lab.collaborator("dev", of=enterprise)
    dev.commit({"enterprise.txt": "feature\n"}, message="feature", branch=BRANCH)
    dev.push(BRANCH)
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "enterprise", "--base", "18.0").exit_code == 0
    before = lab.sha(ws / "community", "HEAD")

    # -n prints the plan and switches nothing (purity: refs and HEADs intact).
    result = lab.ow("switch", BRANCH, "-n", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert f"# community: no {BRANCH}" in lines
    assert f"git -C enterprise fetch origin {BRANCH}" in lines
    assert f"git -C enterprise switch -c {BRANCH} --track origin/{BRANCH}" in lines
    assert f"git -C enterprise config branch.{BRANCH}.ow-base origin/18.0" in lines
    assert not _ok(ws / "enterprise", "symbolic-ref", "-q", "HEAD")
    assert not _ok(
        repos_dir() / "enterprise.git",
        "rev-parse", "--verify", f"refs/remotes/origin/{BRANCH}",
    )
    assert lab.sha(ws / "community", "HEAD") == before

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 0
    assert f"# community: no {BRANCH}" in result.stdout.splitlines()
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == BRANCH
    assert (
        lab.git(ws / "enterprise", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        == f"origin/{BRANCH}"
    )
    assert (
        lab.git(repos_dir() / "enterprise.git", "config", "--get", f"branch.{BRANCH}.ow-base")
        == "origin/18.0"
    )
    # community has the branch nowhere: it stays detached where it was.
    assert not _ok(ws / "community", "symbolic-ref", "-q", "HEAD")
    assert lab.sha(ws / "community", "HEAD") == before


def test_create_with_explicit_base_records_it_and_tracks_nothing(lab):
    ws = _workspace(lab, "community", "enterprise")

    result = lab.ow("switch", "-c", BRANCH, "--base", "18.0", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    for name in ("community", "enterprise"):
        assert f"git -C {name} switch --no-track -c {BRANCH} origin/18.0" in lines
        assert f"git -C {name} config branch.{BRANCH}.ow-base origin/18.0" in lines
        worktree = ws / name
        assert lab.git(worktree, "symbolic-ref", "--short", "HEAD") == BRANCH
        assert (
            lab.git(repos_dir() / f"{name}.git", "config", "--get", f"branch.{BRANCH}.ow-base")
            == "origin/18.0"
        )
        # no upstream: the user's own `git push -u` sets it later (§6)
        assert not _ok(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")


def test_two_remotes_refuse_the_whole_workspace(lab):
    community = lab.remote(
        "community", {"18.0": {"README.md": "c\n"}, BRANCH: {"feature.py": "f\n"}}, head="18.0"
    )
    enterprise = lab.remote("enterprise", {"18.0": {"README.md": "e\n"}}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{community}").exit_code == 0
    assert lab.ow("repo", "add", "enterprise", f"file://{enterprise}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "enterprise", "--base", "18.0").exit_code == 0
    mirror = lab.remote(
        "mirror", {"18.0": {"README.md": "m\n"}, BRANCH: {"feature.py": "f\n"}}, head="18.0"
    )
    before = {name: lab.sha(ws / name, "HEAD") for name in ("community", "enterprise")}
    lab.git(repos_dir() / "community.git", "remote", "add", "mirror", f"file://{mirror}")

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    hint = f"git -C ~/ws/voip/community switch -c {BRANCH} --track origin/{BRANCH}"
    reason = f"{BRANCH} is on remotes mirror, origin; run {hint}"
    assert f"# community: {reason}" in lines
    assert f"# enterprise: not switched: community: {reason}" in lines
    assert not [line for line in lines if line.startswith("git -C")]
    for name in ("community", "enterprise"):
        assert lab.sha(ws / name, "HEAD") == before[name]
        assert not _ok(ws / name, "symbolic-ref", "-q", "HEAD")


def test_a_failure_splits_the_workspace_and_a_rerun_is_idempotent(lab):
    ws = _workspace(lab, "community", "enterprise")
    # a stale config lock lets the branch be created but not its ow-base write
    lock = repos_dir() / "community.git" / "config.lock"
    lock.write_text("")

    try:
        result = lab.ow("switch", "-c", BRANCH, cwd=ws)
    finally:
        lock.unlink()

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert f"git -C community switch --no-track -c {BRANCH} origin/18.0" in lines
    assert f"git -C community config branch.{BRANCH}.ow-base origin/18.0" in lines
    assert "switched: enterprise" in lines
    assert "not switched: community" in lines
    # community's branch was created before the ow-base write failed
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == BRANCH
    assert not _ok(repos_dir() / "community.git", "config", "--get", f"branch.{BRANCH}.ow-base")
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == BRANCH
    assert (
        lab.git(repos_dir() / "enterprise.git", "config", "--get", f"branch.{BRANCH}.ow-base")
        == "origin/18.0"
    )

    result = lab.ow("switch", "-c", BRANCH, cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    # the branch is local now: case 1, so both are already on it
    assert f"git -C community switch {BRANCH}" in lines
    assert f"git -C enterprise switch {BRANCH}" in lines
    assert not [line for line in lines if "not switched" in line]
    for name in ("community", "enterprise"):
        assert lab.git(ws / name, "symbolic-ref", "--short", "HEAD") == BRANCH


def test_detach_moves_every_repo_to_its_default_base(lab):
    ws = _workspace(lab, "community", "enterprise")
    assert lab.ow("switch", "-c", BRANCH, cwd=ws).exit_code == 0

    result = lab.ow("switch", "--detach", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    for name in ("community", "enterprise"):
        assert f"git -C {name} switch --detach origin/18.0" in lines
        assert not _ok(ws / name, "symbolic-ref", "-q", "HEAD")
        assert lab.sha(ws / name, "HEAD") == lab.sha(ws / name, "origin/18.0")


def test_no_repo_has_the_branch_points_at_create(lab):
    ws = _workspace(lab, "community")

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 2
    assert (
        f"error: no repo has {BRANCH}; create it with `ow switch -c {BRANCH}`"
        in result.stderr.splitlines()
    )
    assert not _ok(ws / "community", "symbolic-ref", "-q", "HEAD")
