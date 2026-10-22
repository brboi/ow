"""`ow prune`: machine-wide cleanup of stale worktrees, orphan branches and dead index entries."""

from __future__ import annotations

from pathlib import Path

from ow import term
from ow.core import git, index
from ow.core.paths import repos_dir
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, Step, run_batch
from ow.recipes.removal import branch_removal


def run(*, dry_run: bool, yes: bool) -> int:
    """Survey every bare and the index, then run the one machine-wide batch."""
    plans: list[RepoPlan] = []
    for bare in _bares():
        plan, hints = _survey(bare)
        for line in hints:
            term.out(line)
        if plan is not None:
            plans.append(plan)
    after = tuple(
        OwStep(
            shown=f"# drop {_display(path)} from the index",
            run=lambda entry=path: index.remove(entry),
        )
        for path in _dead_entries()
    )
    outcome = run_batch(
        Batch(root=None, repos=tuple(plans), after=after), prompt=True, dry_run=dry_run, yes=yes
    )
    return outcome.exit_code


def _bares() -> tuple[Path, ...]:
    """Every bare repo of `repos_dir()`, by name; a stray directory is not surveyed."""
    directory = repos_dir()
    if not directory.is_dir():
        return ()
    bares: list[Path] = []
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        if not entry.name.endswith(".git") or entry.name.startswith(".") or not entry.is_dir():
            continue
        result = git.run(["git", "-C", str(entry), "rev-parse", "--is-bare-repository"])
        if result.returncode == 0 and result.stdout.strip() == "true":
            bares.append(entry)
    return tuple(bares)


def _survey(bare: Path) -> tuple[RepoPlan | None, tuple[str, ...]]:
    """What `bare` needs, plus one hint line per prunable worktree."""
    prunable, checked_out = _worktrees(bare)
    hints = tuple(
        f"# {_display(path)}: prunable (moved a workspace? run `ow init` there first)"
        for path in prunable
    )
    steps: list[Step] = []
    if prunable:
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "prune")))
    notes: list[str] = []
    head_branch = _head_branch(bare)
    for branch in _local_branches(bare):
        if branch in checked_out or branch == head_branch:
            continue
        branch_steps, branch_notes = branch_removal(bare, branch)
        steps.extend(branch_steps)
        notes.extend(branch_notes)
    if not steps and not notes:
        return None, hints
    return RepoPlan(repo=bare.stem, steps=tuple(steps), notes=tuple(notes)), hints


def _head_branch(bare: Path) -> str | None:
    """The branch the bare's HEAD points at, or None when it is detached."""
    result = git.run(["git", "-C", str(bare), "symbolic-ref", "--quiet", "--short", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else None


def _worktrees(bare: Path) -> tuple[tuple[Path, ...], frozenset[str]]:
    """(prunable worktree paths, checked-out branches) from one porcelain listing.

    A prunable worktree's branch still counts as checked out: the listing is read before
    anything runs, so the branch pass sees the repo as it is, not as prune will make it.
    """
    listing = git.read(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
    prunable: list[Path] = []
    checked_out: set[str] = set()
    path: Path | None = None
    branch: str | None = None
    is_prunable = False

    def close() -> None:
        nonlocal path, branch, is_prunable
        if path is not None:
            if is_prunable:
                prunable.append(path)
            if branch is not None:
                checked_out.add(branch)
        path, branch, is_prunable = None, None, False

    for line in listing.split("\n"):
        if not line:
            close()
        elif line.startswith("worktree "):
            close()
            path = Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):]
        elif line == "prunable" or line.startswith("prunable "):
            is_prunable = True
    close()
    return tuple(prunable), frozenset(checked_out)


def _local_branches(bare: Path) -> tuple[str, ...]:
    """The bare's local branch names, without the `refs/heads/` prefix.

    `lstrip=2`, not `short`: a tag named like a branch would make `short` print
    `heads/<b>`, a name no ref answers to.
    """
    output = git.read(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads/"]
    )
    return tuple(line.strip() for line in output.split("\n") if line.strip())


def _dead_entries() -> tuple[Path, ...]:
    """The index entries whose path no longer holds `.ow/config.toml`."""
    return tuple(path for path in index.read() if not (path / ".ow" / "config.toml").exists())


def _display(path: Path) -> str:
    """The path as the git lines print it: `~/…` under `$HOME`, else absolute."""
    return git.show(("git", "-C", str(path))).removeprefix("git -C ")
