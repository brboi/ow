"""Pre-flight refusal checks."""

from __future__ import annotations

from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts

# Mutating commands that can't continue if an operation is in progress
MUTATING = frozenset(("rebase", "repo rm", "rm"))


def refusal(
    facts: RepoFacts,
    command: str,
    *,
    autostash: bool = False,
    root: Path | None = None,
    has_target_base: bool = False,
) -> str | None:
    """Return the refusal message for command on this repository, or None."""
    if facts.problem:
        return f"worktree is broken ({facts.problem}); repair or recreate the workspace"

    if command in MUTATING and facts.operation:
        # bisect has no --abort; `git bisect reset` returns to the original HEAD
        abort = ["bisect", "reset"] if facts.operation == "bisect" else [facts.operation, "--abort"]
        hint = git.show(["git", "-C", str(facts.path), *abort], root)
        return f"{facts.operation} in progress; finish it or run `{hint}`"

    if command == "rebase" and not autostash and facts.modified:
        return "modified files; commit, stash, or pass --autostash"

    # Without a base the recipe would take the base for a colleague's branch
    # (a branch) or has nothing to advance to (a detached HEAD).
    if command == "rebase" and facts.base is None and not has_target_base:
        return "no base; run `ow init` to migrate an older workspace, or `ow rebase <base>` to set one"

    return None
