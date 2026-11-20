"""`ow rm <ws>` — remove a workspace: its worktrees, its pushed branches, its directory (§7).

The pre-flight is all-or-nothing: any modified or untracked file in any worktree, or any
repo the shared pre-flight refuses (a broken worktree, an operation in progress), refuses
the whole removal. Each offending file is its own line, exit 1, nothing runs:

    # <repo>: modified <path>
    # <repo>: untracked <path>

The plan then holds, per repo, `git -C <bare> worktree remove <path>` (never `--force`)
and the branch rule — a pushed branch goes with `branch -D`, an unpushed one stays and is
listed as `# <repo>: kept unpushed branch <b>` — followed by the two workspace-level
steps of `Batch.after`:

    # <n> file(s) outside the repos
    rm -rf ~/ws/voip
    ow forget ~/ws/voip

The count is printed first: it is what `rm -rf` takes besides the repos. The last line
drops the index entry; there is no `ow forget` command, the line only names the action.
No plugin hook runs, and no refresh either: the workspace is gone. `-n` prints and
stops; the prompt is §13's; the exit code is the batch's.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Sequence

from ow import term
from ow.core import git, index
from ow.core.facts import RepoFacts, gather_all
from ow.core.plan import Batch, GitStep, OwStep, RepoPlan, run_batch
from ow.core.session import Session
from ow.recipes import preflight
from ow.recipes.removal import branch_is_pushed


def run(session: Session, *, dry_run: bool, yes: bool) -> int:
    """Remove the session's workspace; 1 when the pre-flight refused, else the batch's code."""
    workspace = session.workspace
    assert workspace is not None  # cli opens the positional <ws> with need_workspace=True
    facts = gather_all(session.locations)

    refusals = _refusals(facts)
    if refusals:
        for name, text in refusals:
            term.out(f"# {name}: {text}")
        return 1

    root = workspace.root
    term.out(f"# {_files_outside(root, tuple(repo_facts.path for repo_facts in facts))}"
             " file(s) outside the repos")
    after = (
        OwStep(shown=git.show(("rm", "-rf", str(root))), run=lambda: shutil.rmtree(root)),
        OwStep(shown=git.show(("ow", "forget", str(root))), run=lambda: index.remove(root)),
    )
    outcome = run_batch(
        Batch(
            root=root,
            repos=tuple(_plan(repo_facts) for repo_facts in facts),
            after=after,
        ),
        prompt=True,
        dry_run=dry_run,
        yes=yes,
    )
    return outcome.exit_code


def _refusals(facts: Sequence[RepoFacts]) -> tuple[tuple[str, str], ...]:
    """Why the whole removal is refused: every dirty file, and every shared-rule refusal (§7)."""
    lines: list[tuple[str, str]] = []
    for repo_facts in facts:
        reason = preflight.refusal(repo_facts, "rm")
        if reason is not None:
            lines.append((repo_facts.name, reason))
            continue
        for path in repo_facts.modified:
            lines.append((repo_facts.name, f"modified {path}"))
        for path in repo_facts.untracked:
            lines.append((repo_facts.name, f"untracked {path}"))
    return tuple(lines)


def _plan(repo_facts: RepoFacts) -> RepoPlan:
    """One repo: the worktree goes, and its branch goes only when pushed (§7)."""
    steps = [
        GitStep(("git", "-C", str(repo_facts.bare), "worktree", "remove", str(repo_facts.path)))
    ]
    notes: list[str] = []
    branch = repo_facts.branch
    if branch is not None:
        if branch_is_pushed(repo_facts.bare, branch):
            steps.append(GitStep(("git", "-C", str(repo_facts.bare), "branch", "-D", branch)))
        else:
            notes.append(f"kept unpushed branch {branch}")
    return RepoPlan(repo=repo_facts.name, steps=tuple(steps), notes=tuple(notes))


def _files_outside(root: Path, repos: Sequence[Path]) -> int:
    """The files under `root` that live in no repo worktree: what `rm -rf` takes besides them."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not any(_inside(here / name, repo) for repo in repos)]
        count += len(filenames)
    return count


def _inside(path: Path, repo: Path) -> bool:
    """Whether `path` is `repo` or somewhere under it."""
    try:
        path.relative_to(repo)
    except ValueError:
        return False
    return True
