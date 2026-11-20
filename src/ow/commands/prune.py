"""`ow prune` — machine-wide cleanup: stale worktree references, orphan branches, dead index
entries (§7).

One batch spans the whole machine, so one prompt covers everything. Per bare repo of
`repos_dir()`, the plan holds what that bare needs; the plan's repo names are bare names,
not workspace repos, so the batch displays with `root=None` and every path prints
absolutely or `~`-relative. Each prunable worktree is listed before the plan with the
§7 hint, as its own line:

    # ~/ws/voip/community: prunable (moved a workspace? run `ow init` there first)

An orphan branch — a local branch checked out in no worktree, where a prunable
worktree's branch still counts as checked out — goes with `branch -D` when pushed, and
otherwise stays, listed as `# <bare>: kept unpushed branch <b>`. A dead index entry (a
path that no longer holds `.ow/config.toml`) is forgotten as a workspace-level step,
shown as `ow forget <path>`. Nothing to do prints nothing and asks nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core import git, index
from ow.core.paths import repos_dir
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, run_batch
from ow.recipes.removal import branch_is_pushed

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def run(plugins: "Plugins", *, dry_run: bool, yes: bool) -> int:
    """Survey every bare and the index, then run the one machine-wide batch (§7)."""
    _ = plugins  # prune runs no plugin hook: the core spec gives it none
    plans: list[RepoPlan] = []
    for bare in _bares():
        plan, hints = _survey(bare)
        for line in hints:
            term.out(line)
        if plan is not None:
            plans.append(plan)
    after = tuple(
        OwStep(
            shown=git.show(("ow", "forget", str(path))),
            run=lambda entry=path: index.remove(entry),
        )
        for path in _dead_entries()
    )
    outcome = run_batch(
        Batch(root=None, repos=tuple(plans), after=after),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
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
    """What `bare` needs (§7), plus one hint line per prunable worktree."""
    prunable, checked_out = _worktrees(bare)
    hints = tuple(
        f"# {_display(path)}: prunable (moved a workspace? run `ow init` there first)"
        for path in prunable
    )
    steps: list[GitStep] = []
    if prunable:
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "prune")))
    notes: list[str] = []
    for branch in _local_branches(bare):
        if branch in checked_out:
            continue
        if branch_is_pushed(bare, branch):
            steps.append(GitStep(("git", "-C", str(bare), "branch", "-D", branch)))
        else:
            notes.append(f"kept unpushed branch {branch}")
    if not steps and not notes:
        return None, hints
    return RepoPlan(repo=bare.stem, steps=tuple(steps), notes=tuple(notes)), hints


def _worktrees(bare: Path) -> tuple[tuple[Path, ...], frozenset[str]]:
    """(prunable worktree paths, checked-out branches) from one porcelain listing (§7).

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
    """The bare's local branches, in git's order: plumbing, so no colour can reach a name."""
    output = git.read(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    )
    return tuple(line.strip() for line in output.split("\n") if line.strip())


def _dead_entries() -> tuple[Path, ...]:
    """The index entries whose path no longer holds `.ow/config.toml` (§7)."""
    return tuple(path for path in index.read() if not (path / ".ow" / "config.toml").exists())


def _display(path: Path) -> str:
    """`~/…` under `$HOME`, else absolute: a note, not a command line, so no quoting."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
