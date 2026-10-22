"""`ow switch` scenarios: the enterprise-only branch, `-c --base`, the
all-or-nothing refusal, the split after a failure and its idempotent re-run."""

from __future__ import annotations

from pathlib import Path

from ow.core.paths import repos_dir
from tests.conftest import add_repo, git_ok, init_workspace
from tests.lab import file_url

BRANCH = "18.0-voip"


def _workspace(lab, *names: str) -> Path:
    """A workspace at ~/ws/voip: one registered remote and one detached worktree per name."""
    for name in names:
        add_repo(lab, name, {"18.0": {"README.md": f"{name}\n"}})
    return init_workspace(lab, *names)


def test_branch_only_in_enterprise_switches_it_and_lists_community(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c\n"}})
    enterprise = add_repo(lab, "enterprise", {"18.0": {"README.md": "e\n"}})
    dev = lab.collaborator("dev", of=enterprise)
    dev.commit({"enterprise.txt": "feature\n"}, message="feature", branch=BRANCH)
    dev.push(BRANCH)
    ws = init_workspace(lab, "community", "enterprise")
    before = lab.sha(ws / "community", "HEAD")

    # -n prints the plan and switches nothing (purity: refs and HEADs intact).
    result = lab.ow("switch", BRANCH, "-n", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert f"# community: no {BRANCH}" in lines
    assert f"git -C enterprise fetch origin +refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}" in lines
    assert f"git -C enterprise switch -c {BRANCH} --no-track origin/{BRANCH}" in lines
    assert f"git -C enterprise config branch.{BRANCH}.remote origin" in lines
    assert f"git -C enterprise config branch.{BRANCH}.merge refs/heads/{BRANCH}" in lines
    assert f"git -C enterprise config branch.{BRANCH}.ow-base origin/18.0" in lines
    assert not git_ok(ws / "enterprise", "symbolic-ref", "-q", "HEAD")
    assert not git_ok(
        repos_dir() / "enterprise.git",
        "rev-parse", "--verify", f"refs/remotes/origin/{BRANCH}",
    )
    assert lab.sha(ws / "community", "HEAD") == before

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 0
    assert f"# community: no {BRANCH}" in result.stdout.splitlines()
    assert lab.git(ws / "enterprise", "symbolic-ref", "--short", "HEAD") == BRANCH
    assert (
        lab.git(repos_dir() / "enterprise.git", "config", "--get", f"branch.{BRANCH}.remote")
        == "origin"
    )
    assert (
        lab.git(repos_dir() / "enterprise.git", "config", "--get", f"branch.{BRANCH}.merge")
        == f"refs/heads/{BRANCH}"
    )
    assert (
        lab.git(repos_dir() / "enterprise.git", "config", "--get", f"branch.{BRANCH}.ow-base")
        == "origin/18.0"
    )
    # community has the branch nowhere: it stays detached where it was.
    assert not git_ok(ws / "community", "symbolic-ref", "-q", "HEAD")
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
        # no upstream: the user's own `git push -u` sets it later
        assert not git_ok(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")


def test_create_with_an_uncached_base_fetches_it_first(lab):
    community = lab.remote("community", {"18.0": {"README.md": "c\n"}}, head="18.0")
    dev = lab.collaborator("dev", of=community)
    dev.commit({"dev.txt": "d\n"}, message="dev", branch="dev/base")
    dev.push("dev/base")
    assert lab.ow("repo", "add", "community", file_url(community)).exit_code == 0
    ws = init_workspace(lab, "community")

    # dev/base was pushed by hand: ow never fetched it, so the create must fetch it first
    result = lab.ow("switch", "-c", BRANCH, "--base", "origin/dev/base", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert (
        "git -C community fetch origin +refs/heads/dev/base:refs/remotes/origin/dev/base" in lines
    )
    assert f"git -C community switch --no-track -c {BRANCH} origin/dev/base" in lines
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == BRANCH
    assert (
        lab.git(repos_dir() / "community.git", "config", "--get", f"branch.{BRANCH}.ow-base")
        == "origin/dev/base"
    )


def test_two_remotes_refuse_the_whole_workspace(lab):
    add_repo(
        lab, "community", {"18.0": {"README.md": "c\n"}, BRANCH: {"feature.py": "f\n"}}
    )
    add_repo(lab, "enterprise", {"18.0": {"README.md": "e\n"}})
    ws = init_workspace(lab, "community", "enterprise")
    mirror = lab.remote(
        "mirror", {"18.0": {"README.md": "m\n"}, BRANCH: {"feature.py": "f\n"}}, head="18.0"
    )
    before = {name: lab.sha(ws / name, "HEAD") for name in ("community", "enterprise")}
    lab.git(repos_dir() / "community.git", "remote", "add", "mirror", file_url(mirror))

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    hint = (
        f"git -C community fetch origin +refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}; "
        f"git -C community switch -c {BRANCH} --no-track origin/{BRANCH}; "
        f"git -C community config branch.{BRANCH}.remote origin; "
        f"git -C community config branch.{BRANCH}.merge refs/heads/{BRANCH}"
    )
    reason = f"{BRANCH} is on remotes mirror, origin; run {hint}"
    assert f"# community: {reason}" in lines
    assert "# enterprise: nothing switched" in lines
    assert not [line for line in lines if line.startswith("git -C")]
    for name in ("community", "enterprise"):
        assert lab.sha(ws / name, "HEAD") == before[name]
        assert not git_ok(ws / name, "symbolic-ref", "-q", "HEAD")


def test_an_unreachable_remote_refuses_the_whole_workspace(lab):
    ws = _workspace(lab, "community", "enterprise")
    lab.git(
        repos_dir() / "community.git",
        "remote", "set-url", "origin", "file:///nonexistent/community.git",
    )

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert any(line.startswith("# community: ls-remote origin failed:") for line in lines)
    assert "# enterprise: nothing switched" in lines
    assert not [line for line in lines if f"create it with `ow switch -c {BRANCH}`" in line]
    assert not [line for line in lines if line.startswith("git -C")]


def test_a_detached_head_with_orphan_commits_refuses_the_switch(lab):
    ws = _workspace(lab, "community")
    worktree = ws / "community"
    (worktree / "wip.txt").write_text("wip\n")
    lab.git(worktree, "add", "wip.txt")
    lab.git(worktree, "commit", "-m", "wip")
    before = lab.sha(worktree, "HEAD")

    result = lab.ow("switch", "-c", BRANCH, cwd=ws)

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert (
        f"# community: commits on no branch; save them with "
        f"`git -C community switch -c <b>`" in lines
    )
    assert lab.sha(worktree, "HEAD") == before
    assert not git_ok(worktree, "symbolic-ref", "-q", "HEAD")


def test_a_suffixed_branch_on_the_remote_does_not_match(lab):
    add_repo(lab, "community", {"18.0": {"README.md": "c\n"}, "dev/feat": {"f.py": "f\n"}})
    ws = init_workspace(lab, "community")

    result = lab.ow("switch", "feat", cwd=ws)

    assert result.exit_code == 2
    assert (
        f"error: no repo has feat; create it with `ow switch -c feat`"
        in result.stderr.splitlines()
    )
    assert not [line for line in result.stdout.splitlines() if line.startswith("git -C")]


def test_dirty_worktree_is_carried_across_the_switch(lab):
    ws = _workspace(lab, "community")
    (ws / "community" / "README.md").write_text("dirty\n")

    result = lab.ow("switch", "-c", BRANCH, cwd=ws)

    assert result.exit_code == 0
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == BRANCH
    assert (ws / "community" / "README.md").read_text() == "dirty\n"
    assert "README.md" in lab.git(ws / "community", "status", "--porcelain")


def test_only_limits_the_switch_to_one_repo(lab):
    ws = _workspace(lab, "community", "enterprise")

    result = lab.ow("switch", "-c", BRANCH, "--only", "community", cwd=ws)

    assert result.exit_code == 0
    assert lab.git(ws / "community", "symbolic-ref", "--short", "HEAD") == BRANCH
    # enterprise was left out: it stays detached
    assert not git_ok(ws / "enterprise", "symbolic-ref", "-q", "HEAD")
    assert f"git -C enterprise" not in result.stdout


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
    assert not git_ok(repos_dir() / "community.git", "config", "--get", f"branch.{BRANCH}.ow-base")
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
    # community's missing ow-base is repaired; enterprise's is already there
    assert f"git -C community config branch.{BRANCH}.ow-base origin/18.0" in lines
    assert f"git -C enterprise config branch.{BRANCH}.ow-base origin/18.0" not in lines
    assert not [line for line in lines if "not switched" in line]
    for name in ("community", "enterprise"):
        assert lab.git(ws / name, "symbolic-ref", "--short", "HEAD") == BRANCH
        assert (
            lab.git(repos_dir() / f"{name}.git", "config", "--get", f"branch.{BRANCH}.ow-base")
            == "origin/18.0"
        )


def test_detach_moves_every_repo_to_its_default_base(lab):
    ws = _workspace(lab, "community", "enterprise")
    assert lab.ow("switch", "-c", BRANCH, cwd=ws).exit_code == 0

    result = lab.ow("switch", "--detach", cwd=ws)

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    for name in ("community", "enterprise"):
        assert f"git -C {name} switch --detach origin/18.0" in lines
        assert not git_ok(ws / name, "symbolic-ref", "-q", "HEAD")
        assert lab.sha(ws / name, "HEAD") == lab.sha(ws / name, "origin/18.0")


def test_branch_checked_out_in_another_worktree_refuses(lab):
    ws = _workspace(lab, "community")
    bare = repos_dir() / "community.git"
    lab.git(bare, "worktree", "add", "-b", BRANCH, str(ws.parent / "community-extra"), "origin/18.0")

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 1
    assert (
        f"# community: {BRANCH} is checked out in another worktree"
        in result.stdout.splitlines()
    )
    assert not git_ok(ws / "community", "symbolic-ref", "-q", "HEAD")


def test_create_with_base_on_an_existing_branch_notes_it(lab):
    ws = _workspace(lab, "community")
    assert lab.ow("switch", "-c", BRANCH, cwd=ws).exit_code == 0

    result = lab.ow("switch", "-c", BRANCH, "--base", "origin/master", cwd=ws)

    assert result.exit_code == 0
    assert f"# community: --base ignored; {BRANCH} already exists" in result.stdout.splitlines()


def test_an_operation_in_progress_refuses_the_switch(lab):
    ws = _workspace(lab, "community")
    worktree = ws / "community"
    gitdir = Path(lab.git(worktree, "rev-parse", "--absolute-git-dir"))
    (gitdir / "MERGE_HEAD").write_text(lab.sha(worktree, "HEAD") + "\n")

    result = lab.ow("switch", "-c", BRANCH, cwd=ws)

    assert result.exit_code == 1
    assert any(
        line.startswith("# community: merge in progress; finish it or run")
        for line in result.stdout.splitlines()
    )
    assert not git_ok(worktree, "symbolic-ref", "-q", "HEAD")


def test_no_repo_has_the_branch_points_at_create(lab):
    ws = _workspace(lab, "community")

    result = lab.ow("switch", BRANCH, cwd=ws)

    assert result.exit_code == 2
    assert (
        f"error: no repo has {BRANCH}; create it with `ow switch -c {BRANCH}`"
        in result.stderr.splitlines()
    )
    assert not git_ok(ws / "community", "symbolic-ref", "-q", "HEAD")
