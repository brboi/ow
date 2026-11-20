"""`ow rm <ws>`: remove a workspace's worktrees, its pushed branches and its directory."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from ow import term
from ow.commands.prune import _display
from ow.core import git, index
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, Step, run_batch
from ow.core.session import Session
from ow.core.workspace import RepoLocation, stray_checkouts
from ow.recipes import preflight
from ow.recipes.removal import branch_removal, orphan_commits

DIRTY = "commit, stash or discard, then re-run"


def run(session: Session, *, dry_run: bool, yes: bool) -> int:
    """Remove the session's workspace; 1 when the pre-flight refused, else the batch's code."""
    workspace = session.workspace
    assert workspace is not None  # cli opens the positional <ws> with need_workspace=True
    root = workspace.root
    facts = gather_all(session.locations)

    refusals = _refusals(facts, root, session.locations)
    if refusals:
        for name, text in refusals:
            term.out(f"# {name}: {text}")
        return 1

    outside = _outside(root, tuple(repo_facts.path for repo_facts in facts))
    if outside:
        term.out(f"# {outside}")

    after = (
        OwStep(shown=git.show(("rm", "-rf", str(root))), run=lambda: shutil.rmtree(root)),
        OwStep(shown=f"# drop {_display(root)} from the index", run=lambda: index.remove(root)),
    )
    outcome = run_batch(
        Batch(root=root, repos=tuple(_plan(repo_facts) for repo_facts in facts), after=after),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    return outcome.exit_code


def _refusals(
    facts: Sequence[RepoFacts], root: Path, locations: Sequence[RepoLocation]
) -> tuple[tuple[str, str], ...]:
    """Why the whole removal is refused: dirty files, unsafe HEADs, unmanaged checkouts."""
    lines: list[tuple[str, str]] = []
    for repo_facts in facts:
        reason = preflight.refusal(repo_facts, "rm", root=root)
        if reason is not None:
            lines.append((repo_facts.name, reason))
            continue
        for path in repo_facts.modified:
            lines.append((repo_facts.name, f"modified {path}; {DIRTY}"))
        for path in repo_facts.untracked:
            lines.append((repo_facts.name, f"untracked {path}; {DIRTY}"))
        if repo_facts.branch is None and repo_facts.head is not None:
            orphans = orphan_commits(repo_facts.bare, repo_facts.head)
            if orphans:
                lines.append(
                    (
                        repo_facts.name,
                        f"{orphans} commit(s) on no branch; keep them with "
                        f"`git -C {_display(repo_facts.path)} switch -c <b>`, or discard them,"
                        " then re-run",
                    )
                )
    for stray in stray_checkouts(root, locations):
        lines.append((stray.name, "a checkout ow does not manage; move it away or delete it, then re-run"))
    return tuple(lines)


def _plan(repo_facts: RepoFacts) -> RepoPlan:
    """One repo: the worktree goes, and its branch goes only when pushed."""
    steps: list[Step] = [
        GitStep(("git", "-C", str(repo_facts.bare), "worktree", "remove", str(repo_facts.path)))
    ]
    notes: tuple[str, ...] = ()
    if repo_facts.branch is not None:
        branch_steps, notes = branch_removal(repo_facts.bare, repo_facts.branch)
        steps.extend(branch_steps)
    return RepoPlan(repo=repo_facts.name, steps=tuple(steps), notes=notes)


def _outside(root: Path, repos: Sequence[Path]) -> str:
    """The top-level entries `rm -rf` takes besides the repo worktrees, with file counts."""
    entries: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if any(entry.is_relative_to(repo) for repo in repos):
            continue
        if not entry.is_dir():
            entries.append(entry.name)
            continue
        count = sum(1 for child in entry.rglob("*") if child.is_file())
        plural = "s" if count != 1 else ""
        entries.append(f"{entry.name}/ ({count} file{plural})" if count else f"{entry.name}/")
    return ", ".join(entries)
