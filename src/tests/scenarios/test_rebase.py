"""`ow rebase` scenarios (§7/§16): the shared-branch guard, the conflict tail,
`--base`, `--autostash`, and the purity of `-n` and a declined prompt.

The workspace's branch tracks the branch the collaborator publishes, so a
colleague's push shows up as new upstream commits the guard must take in
without rewriting them. `ow rebase -n` fetches (remote-tracking refs are the
one thing a dry run may move), then prints the plan and writes nothing else;
an `n` answer leaves the workspace byte-identical and exits 2.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ow.core.paths import index_file, repos_dir

BARE = "~/.local/share/ow/repos"
BRANCH = "18.0-voip-boi"


def _workspace(lab, branches: dict[str, dict[str, str]] | None = None):
    """A workspace on `18.0-voip-boi`, tracking the branch the collaborator publishes."""
    remote = lab.remote(
        "community",
        branches if branches is not None else {"18.0": lab.odoo_files()},
        head="18.0",
    )
    assert lab.ow("repo", "add", "community", f"file://{remote}").exit_code == 0
    collaborator = lab.collaborator("dev", of=remote)
    collaborator.commit({"shared.py": "shared\n"}, message="shared", branch=BRANCH)
    collaborator.push(BRANCH)
    ws = lab.ws("voip")
    assert (
        lab.ow("init", str(ws), "community", "--base", "18.0", "--branch", BRANCH).exit_code
        == 0
    )
    return ws, ws / "community", collaborator


def _local_commit(lab, worktree: Path) -> str:
    """One unpushed commit on the worktree's branch; returns its sha."""
    (worktree / "local.py").write_text("local\n")
    lab.git(worktree, "add", "local.py")
    lab.git(worktree, "commit", "-m", "local work")
    return lab.git(worktree, "rev-parse", "HEAD")


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by relative path — the workspace tree and the lock."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _index() -> bytes:
    """The workspace index file's bytes."""
    path = index_file()
    return path.read_bytes() if path.exists() else b""


def _refs(lab, worktree: Path) -> dict[str, str]:
    """The worktree's refs, minus the remote-tracking ones a fetch may move."""
    output = lab.git(worktree, "for-each-ref", "--format=%(refname) %(objectname)")
    return {
        name: sha
        for name, sha in (line.split(" ", 1) for line in output.splitlines())
        if not name.startswith("refs/remotes/")
    }


def _ok(path: Path, *args: str) -> bool:
    """Whether a git command in `path` succeeds; a failure is a state under test."""
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True).returncode == 0


def _git_path(worktree: Path, name: str) -> Path:
    """A worktree's git path, resolved (git prints it relative to the -C directory)."""
    raw = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-path", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = Path(raw)
    return path if path.is_absolute() else worktree / path


def test_colleague_pushed_lands_the_new_commits_with_a_cherry_pick(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --empty=drop {taken}..origin/{BRANCH}",
    ]

    # The colleague's commit landed above the local one; nothing is left over.
    assert lab.git(worktree, "log", "--format=%s", "-2").splitlines() == [
        "colleague",
        "local work",
    ]
    assert (worktree / "colleague.py").read_text() == "colleague\n"
    assert not _git_path(worktree, "CHERRY_PICK_HEAD").exists()

    # A re-run is a no-op: the patch is already in, so the plan is empty.
    head = lab.git(worktree, "rev-parse", "HEAD")
    again = lab.ow("rebase", "-y", cwd=ws)

    assert again.exit_code == 0
    assert again.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}"
    ]
    assert lab.git(worktree, "rev-parse", "HEAD") == head


def test_mixed_force_push_and_exhausted_reflog_falls_back_to_fork_point(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    # The colleague rewrites the shared branch from the base, so no upstream
    # value this branch ever took in survives the force-push.
    collaborator.git("reset", "--hard", lab.sha(collaborator.path, "origin/18.0"))
    collaborator.commit({"rewritten.py": "rewritten\n"}, message="rewritten")
    collaborator.git("push", "--force", "origin", BRANCH)

    # Take the rewrite in, then let the reflog's unreachable entries expire:
    # nothing the branch ever took in is left to fast-forward from.
    bare = repos_dir() / "community.git"
    lab.git(bare, "fetch", "origin", BRANCH)
    lab.git(bare, "reflog", "expire", "--expire-unreachable=now",
            f"refs/remotes/origin/{BRANCH}")

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        f"git -C community rebase --fork-point origin/{BRANCH}",
    ]
    assert _ok(worktree, "merge-base", "--is-ancestor", f"origin/{BRANCH}", "HEAD")
    assert (worktree / "rewritten.py").read_text() == "rewritten\n"
    assert not _git_path(worktree, "rebase-merge").exists()


def test_a_conflict_prints_the_continuation_then_the_remaining_commands(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)

    # The colleague moves the base with a change the branch's own commit clashes with.
    collaborator.git("checkout", "18.0")
    collaborator.commit({"shared.py": "base\n"}, message="base shared")
    collaborator.push("18.0")

    result = lab.ow("rebase", "-y", cwd=ws)

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        "git -C community rebase origin/18.0",
        "git -C community rebase --continue",
        "git -C community rebase --abort",
    ]
    assert _git_path(worktree, "rebase-merge").exists()

    # Resolving by hand and running the printed command completes the rebase.
    (worktree / "shared.py").write_text("resolved\n")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "shared.py"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "rebase", "--continue"],
        check=True, capture_output=True,
        env={**os.environ, "GIT_EDITOR": "true"},
    )
    assert lab.git(worktree, "log", "--format=%s", "-2").splitlines() == [
        "local work",
        "shared",
    ]
    assert _ok(worktree, "merge-base", "--is-ancestor", "origin/18.0", "HEAD")
    assert not _git_path(worktree, "rebase-merge").exists()


def test_a_new_base_is_recorded_only_after_a_successful_rebase(lab):
    # `lab.remote` stacks its branches, so the clean one must come before the
    # conflicting one: 18.0-clean adds only a README line, 18.0-next also a shared.py.
    ws, worktree, collaborator = _workspace(
        lab,
        branches={
            "18.0": lab.odoo_files(),
            "18.0-clean": {"README.md": "clean\n"},
            "18.0-next": {"shared.py": "next\n"},
        },
    )
    _local_commit(lab, worktree)
    bare = repos_dir() / "community.git"
    # `--base` names a ref ow does not fetch itself, so cache both candidates.
    lab.git(bare, "fetch", "origin", "18.0-clean", "18.0-next")

    conflict = lab.ow("rebase", "--base", "origin/18.0-next", "-y", cwd=ws)

    assert conflict.exit_code == 1
    assert conflict.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        "git -C community rebase --onto origin/18.0-next origin/18.0",
        f"git -C community config branch.{BRANCH}.ow-base origin/18.0-next",
        "git -C community rebase --continue",
        "git -C community rebase --abort",
        f"git -C community config branch.{BRANCH}.ow-base origin/18.0-next",
    ]
    # The rebase stopped, so the recorded base kept its old value.
    assert lab.git(bare, "config", "--get", f"branch.{BRANCH}.ow-base") == "origin/18.0"

    lab.git(worktree, "rebase", "--abort")

    clean = lab.ow("rebase", "--base", "origin/18.0-clean", "-y", cwd=ws)

    assert clean.exit_code == 0
    assert clean.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        "git -C community rebase --onto origin/18.0-clean origin/18.0",
        f"git -C community config branch.{BRANCH}.ow-base origin/18.0-clean",
    ]
    assert lab.git(bare, "config", "--get", f"branch.{BRANCH}.ow-base") == "origin/18.0-clean"
    assert _ok(worktree, "merge-base", "--is-ancestor", "origin/18.0-clean", "HEAD")


def test_autostash_keeps_the_stash_and_prints_the_pop_last(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    collaborator.git("checkout", "18.0")
    collaborator.commit({"shared.py": "base\n"}, message="base shared")
    collaborator.push("18.0")
    (worktree / "odoo-bin").write_text("dirty\n")

    result = lab.ow("rebase", "--autostash", "-y", cwd=ws)

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        "git -C community stash push -m 'ow rebase --autostash'",
        "git -C community rebase origin/18.0",
        "git -C community stash pop",
        "git -C community rebase --continue",
        "git -C community rebase --abort",
        "git -C community stash pop",
    ]
    # The rebase stopped before the pop, so the stash is kept for the resume.
    assert "ow rebase --autostash" in lab.git(worktree, "stash", "list")
    assert _git_path(worktree, "rebase-merge").exists()


def test_dry_run_prints_the_plan_and_moves_nothing(lab):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)

    tree = _tree(ws)
    index = _index()
    head = lab.git(worktree, "rev-parse", "HEAD")
    refs = _refs(lab, worktree)

    result = lab.ow("rebase", "-n", cwd=ws)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        f"git -C {BARE}/community.git fetch origin 18.0 {BRANCH}",
        f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in",
        f"git -C community cherry-pick --empty=drop {taken}..origin/{BRANCH}",
    ]

    # Purity: the fetch moved the remote-tracking ref; nothing else changed.
    assert _tree(ws) == tree
    assert _index() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert _refs(lab, worktree) == refs


def test_a_declined_prompt_changes_nothing(lab, monkeypatch):
    ws, worktree, collaborator = _workspace(lab)
    _local_commit(lab, worktree)
    taken = lab.sha(repos_dir() / "community.git", f"refs/remotes/origin/{BRANCH}")
    collaborator.commit({"colleague.py": "colleague\n"}, message="colleague")
    collaborator.push(BRANCH)

    tree = _tree(ws)
    index = _index()
    head = lab.git(worktree, "rev-parse", "HEAD")
    refs = _refs(lab, worktree)

    # P9: the prompt is offered only when `ow.term.interactive()` is true;
    # `run_batch` calls it through the module, so one patch site is enough.
    monkeypatch.setattr("ow.term.interactive", lambda: True)

    result = lab.ow("rebase", input="n\n", cwd=ws)

    assert result.exit_code == 2
    lines = result.stdout.splitlines()
    assert lines[1] == f"# {taken[:10]} = origin/{BRANCH} as this branch last took it in"
    assert lines[2] == f"git -C community cherry-pick --empty=drop {taken}..origin/{BRANCH}"

    assert _tree(ws) == tree
    assert _index() == index
    assert lab.git(worktree, "rev-parse", "HEAD") == head
    assert _refs(lab, worktree) == refs
